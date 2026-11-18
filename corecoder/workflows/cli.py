"""Graph routes with explicit trusted configuration and local status inspection."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ..knowledge.cli import _error, _service, chat_backend, common_arguments
from ..knowledge.models import KnowledgeError, relative_path
from ..knowledge.scope import is_reparse
from ..knowledge.settings import load_settings
from ..memory.cli import service_for_root
from .budget_ledger import WorkflowBudget
from .claims import ClaimBusy
from .state import WorkflowError


def _settings(args):
    options = vars(args).copy()
    if args.offline_fixture:
        options["provider"] = "fixture"
    return load_settings(options)


def _directory(settings, args):
    config = settings.project_config.get("workflow", {})
    if not isinstance(config, dict):
        raise WorkflowError("configuration_error", "workflow configuration must be a TOML table")
    value = (getattr(args, "workflow_data_root", None) or os.getenv("CORECODER_WORKFLOW_DATA_ROOT")
             or config.get("persist_directory", ".corecoder/workflows"))
    path = Path(value)
    return (path if path.is_absolute() else settings.root / path).resolve()


def _verification(settings):
    from ..verification import FileStateVerifier, TestResultVerifier

    config = settings.project_config.get("workflow", {}).get("verification", {})
    if not isinstance(config, dict):
        raise WorkflowError("configuration_error", "workflow verification must be a TOML table")
    if not config:
        return "", None, {}
    policy_id = config.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id or len(policy_id) > 128:
        raise WorkflowError("configuration_error", "verification requires a named trusted policy_id")
    if config.get("type") == "file_state":
        relative = relative_path(config["path"])
        target = settings.root / relative
        target.resolve().relative_to(settings.root)
        current = target
        while current != settings.root:
            if current.exists() and is_reparse(current):
                raise WorkflowError("configuration_error", "verification paths must not traverse reparse points")
            current = current.parent
        verifier = FileStateVerifier(target, content_equals=config.get("content_equals"),
                                     content_contains=config.get("content_contains"), expected_sha256=config.get("expected_sha256"))
    elif config.get("type") == "test_command":
        timeout = float(config.get("timeout_seconds", 120))
        if not 0 < timeout <= 300:
            raise ValueError("verification timeout is invalid")
        verifier = TestResultVerifier(config["command"], timeout_seconds=timeout, cwd=settings.root)
    else:
        raise WorkflowError("configuration_error", "verification type must be file_state or test_command")
    return policy_id, verifier, config


def configured_workflow(settings, args, *, intent):
    from ..agent import Agent
    from ..integrations.agent_llm_bridge import AgentLLMBridge
    from ..integrations.model_gateway import ModelGateway
    from ..permissions import Permission
    from ..run_store import RunStore
    from ..tools import ALL_TOOLS
    from .fixtures import FixtureWorkflowBackend
    from .graph import GraphWorkflow

    backend, output_limit, rates = chat_backend(settings, args)
    if args.offline_fixture:
        backend = FixtureWorkflowBackend(intent)
    policy_id, verifier, policy = _verification(settings)
    runs = RunStore(settings.root / ".corecoder/runs.db")
    yes = getattr(args, "yes", False)

    def agent_factory(gateway, cancellation):
        return Agent(AgentLLMBridge(gateway, cancellation_token=cancellation), tools=ALL_TOOLS,
                     permission=Permission(allow_all=yes), run_store=runs)

    criteria = settings.project_config.get("workflow", {}).get("success_criteria", [])
    if not isinstance(criteria, list) or any(not isinstance(item, str) for item in criteria):
        raise WorkflowError("configuration_error", "success_criteria must be a trusted string list")
    profile = {"model": backend.model, "backend": type(backend).__name__,
               "endpoint": str(getattr(getattr(backend, "client", None), "base_url", getattr(backend, "base_url", ""))),
               "rates": asdict(rates) if rates else None, "output_limit": output_limit,
               "embedding": asdict(settings.embedding), "dimensions": settings.dimensions,
               "embedding_revision": settings.model_revision, "embedding_rate": settings.embedding_rate,
               "embedding_limits": [settings.embedding_batch_size, settings.embedding_max_text_bytes,
                                    settings.embedding_max_batch_bytes],
               "splitter": asdict(settings.splitter()), "verification": policy, "allow_all": yes,
               "knowledge_directory": str(settings.scope().data_root)}
    return GraphWorkflow(settings.scope(), gateway_factory=lambda ledger: ModelGateway(backend, ledger, max_output_tokens=output_limit, rates=rates),
        knowledge_factory=lambda ledger: _service(settings, settings.gateway(ledger, required=False)),
        agent_factory=agent_factory, trusted_configuration=profile, policy_id=policy_id, verifier=verifier,
        success_criteria=criteria, memory_service=service_for_root(settings.root), data_root=_directory(settings, args),
        use_memory=args.memory)


def _write(outcome, args, *, inspect=False):
    value = outcome.to_dict()
    if args.json:
        print(json.dumps(value, ensure_ascii=False))
    else:
        print(f"workflow={outcome.workflow_id} status={outcome.workflow_status} stage={outcome.stage}")
        if outcome.fixture:
            print("backend=offline_fixture (pipeline demonstration)")
        print(f"answer_status={outcome.answer_status} code_run_status={outcome.code_run_status} child_run_id={outcome.child_run_id}")
        if outcome.result:
            print(outcome.result.get("answer") or outcome.result.get("model_text") or outcome.result.get("status", ""))
            for source in outcome.result.get("sources", []):
                print(source["citation"])
        print(f"requests={outcome.budget['requests']} provider_cost_usd=" +
              ("unavailable" if outcome.budget["cost_unavailable"] else str(outcome.budget["cost_usd"])))
        if outcome.error_category:
            print("error_category=" + outcome.error_category)
    if inspect:
        return 0
    return {"completed": 0, "budget_exceeded": 6, "paused": 8, "needs_action": 8, "cancelled": 130}.get(outcome.workflow_status, 1)


def ask_graph(args):
    settings = _settings(args)
    workflow = configured_workflow(settings, args, intent="ask")
    result = workflow.run(args.question, thread_id=args.thread_id or uuid4().hex, turn_id=args.turn_id or uuid4().hex,
        mode=args.mode or settings.retrieval_mode, top_k=args.top_k if args.top_k is not None else settings.context_top_k,
        current_memory_keys=tuple(args.current_memory_key), budget=WorkflowBudget(args.max_requests, args.max_tokens, args.deadline, args.max_cost))
    return _write(result, args)


def _options(parser):
    common_arguments(parser)
    parser.set_defaults(max_requests=24, max_tokens=250_000, deadline=300)
    parser.add_argument("--workflow-data-root")
    parser.add_argument("--offline-fixture", action="store_true")
    parser.add_argument("--chat-model")
    parser.add_argument("--chat-base-url")
    parser.add_argument("--memory", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--yes", action="store_true", help="Explicitly allow child tools; same meaning as the existing CLI")


def main(argv=None, *, route="workflow") -> int:
    parser = argparse.ArgumentParser(prog="corecoder " + route, description=__doc__)
    if route == "run":
        _options(parser)
        parser.add_argument("goal")
        parser.add_argument("--engine", choices=["graph"], default="graph")
        parser.add_argument("--thread-id")
        parser.add_argument("--turn-id")
        parser.add_argument("--mode", choices=["lexical", "vector", "hybrid"])
    else:
        commands = parser.add_subparsers(dest="command", required=True)
        for name in ("status", "resume"):
            command = commands.add_parser(name)
            _options(command)
            command.add_argument("workflow_id")
    args = parser.parse_args(argv)
    try:
        from .graph import GraphWorkflow

        settings = _settings(args)
        if route == "workflow" and args.command == "status":
            result = GraphWorkflow(settings.scope(), data_root=_directory(settings, args)).status(args.workflow_id)
            return _write(result, args, inspect=True)
        if route == "workflow":
            reader = GraphWorkflow(settings.scope(), data_root=_directory(settings, args))
            operation = reader.operations.get(reader.scope, args.workflow_id)
            request = reader.payloads.get(reader.scope, operation["request_ref"], "request")
            if request["intent"] == "code" and settings.root != Path.cwd().resolve():
                raise WorkflowError("configuration_error", "resume a coding workflow from its original project root")
            workflow = configured_workflow(settings, args, intent=request["intent"])
            return _write(workflow.resume(args.workflow_id, resume_paused=True), args)
        if settings.root != Path.cwd().resolve():
            raise WorkflowError("configuration_error", "run from the project root so existing coding tools use the intended working directory")
        workflow = configured_workflow(settings, args, intent="code")
        result = workflow.run(args.goal, intent="code", thread_id=args.thread_id or uuid4().hex, turn_id=args.turn_id or uuid4().hex,
            mode=args.mode or settings.retrieval_mode, budget=WorkflowBudget(args.max_requests, args.max_tokens, args.deadline, args.max_cost))
        return _write(result, args)
    except (WorkflowError, KnowledgeError) as exc:
        return _error(exc, as_json=args.json)
    except ClaimBusy:
        return _error(WorkflowError("needs_action", "workflow is owned by another local process"), as_json=args.json)
    except ImportError:
        return _error(WorkflowError("configuration_error", 'graph commands require corecoder[knowledge,graph]'), as_json=args.json)
    except (ValueError, TypeError, KeyError, OSError):
        return _error(WorkflowError("configuration_error", "workflow configuration or request is invalid"), as_json=args.json)
    except Exception:  # noqa: BLE001 - never echo model/source/provider text to stdout
        return _error(WorkflowError("internal_error", "workflow operation could not be completed"), as_json=args.json)
