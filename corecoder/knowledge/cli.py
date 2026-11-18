"""Local index/search and repository question routes, before Chat key checks."""

from __future__ import annotations

import argparse
import json
import sys

from ..workflows.budget_ledger import BudgetExceeded, BudgetLedger, WorkflowBudget
from .models import KnowledgeError
from .service import KnowledgeService
from .settings import load_settings

_EXIT_CODES = {"configuration_error": 2, "index_missing": 3, "stale_evidence": 4, "embedding_unavailable": 5,
               "budget_exceeded": 6, "index_busy": 7, "needs_action": 8,
               "index_corrupt": 10, "index_incompatible": 10, "index_unavailable": 10}


def common_arguments(parser):
    parser.add_argument("--root", help="Authorized project root")
    parser.add_argument("--data-root", dest="persist_directory", help="Knowledge data directory, relative to the project or absolute")
    parser.add_argument("--embedding-provider", dest="provider", choices=["openai", "fixture"])
    parser.add_argument("--embedding-model", dest="model")
    parser.add_argument("--embedding-dimensions", dest="dimensions", type=int)
    parser.add_argument("--embedding-base-url", dest="base_url")
    parser.add_argument("--embedding-api-key-env", dest="api_key_env")
    parser.add_argument("--embedding-batch-size", dest="batch_size", type=int)
    parser.add_argument("--embedding-max-text-bytes", dest="max_text_bytes", type=int)
    parser.add_argument("--embedding-max-batch-bytes", dest="max_batch_bytes", type=int)
    parser.add_argument("--json", action="store_true", help="Write a single JSON result to stdout")
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--max-tokens", type=int, default=2_000_000)
    parser.add_argument("--deadline", type=float, default=300)
    parser.add_argument("--max-cost", type=float, help="USD cap; requires separate trusted Chat/Embedding rates")


def _write(result: dict, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    if "hits" in result:
        print(f"snapshot={result['snapshot_id']} mode={result['mode']}")
        for hit in result["hits"]:
            ref = hit["citation"]
            print(f"[{ref['source_path']}:L{ref['start_line']}-L{ref['end_line']} @ {ref['snapshot_id']}]")
            print(hit["text"].strip())
        if result["degraded"]:
            print("degraded: " + ", ".join(result["degraded"]))
    elif "answer" in result:
        print(f"status={result['status']} mode={result['mode']} snapshot={result['snapshot_id']}")
        if result.get("fixture"):
            print("backend=offline_fixture (pipeline demonstration)")
        print(result["answer"] or result["status"])
        for source in result["sources"]:
            print(source["citation"])
        for unknown in result["unknowns"]:
            print("unknown: " + unknown)
        if result["degraded"]:
            print("degraded: " + ", ".join(result["degraded"]))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if "budget" in result and ("hits" in result or "answer" in result):
        budget = result["budget"]
        cost = "unavailable" if budget["cost_unavailable"] else str(budget["cost_usd"])
        print(f"requests={budget['requests']} provider_cost_usd={cost}")


def _error(exc, *, as_json: bool) -> int:
    category = "budget_exceeded" if isinstance(exc, BudgetExceeded) else exc.category
    result = {"status": "error", "error": {"category": category, "message": str(exc)}}
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"{category}: {exc}", file=sys.stderr)
    return _EXIT_CODES.get(category, 1)


def _service(settings, gateway=None):
    return KnowledgeService(settings.scope(), gateway, splitter=settings.splitter(), max_file_bytes=settings.max_file_bytes,
                            lexical_top_k=settings.lexical_top_k, vector_top_k=settings.vector_top_k)


def chat_backend(settings, args):
    """Resolve Chat only for an explicit generation command, with independent rates."""
    import os

    from ..config import Config
    from ..llm import LLM, LiteLLM
    from ..workflows.budget_ledger import TokenRates
    from .answering import FixtureAnswerBackend

    if args.offline_fixture:
        return FixtureAnswerBackend(), 1024, None
    config = Config.from_env()
    chat = settings.project_config.get("chat", {})
    if not isinstance(chat, dict):
        raise KnowledgeError("configuration_error", "chat configuration must be a TOML table")
    key_env = chat.get("api_key_env")
    key = os.getenv(key_env, "") if isinstance(key_env, str) else config.api_key
    if not key:
        raise KnowledgeError("configuration_error", "configure the Chat provider before using generation")
    model = args.chat_model or os.getenv("CORECODER_MODEL") or chat.get("model") or config.model
    base_url = args.chat_base_url or os.getenv("CORECODER_BASE_URL") or os.getenv("OPENAI_BASE_URL") or chat.get("base_url")
    provider = os.getenv("CORECODER_PROVIDER") or chat.get("provider") or config.provider
    backend = (LiteLLM if provider == "litellm" else LLM)(model=model, api_key=key, base_url=base_url)
    input_rate = os.getenv("CORECODER_CHAT_INPUT_PER_MILLION") or chat.get("input_per_million")
    output_rate = os.getenv("CORECODER_CHAT_OUTPUT_PER_MILLION") or chat.get("output_per_million")
    rates = TokenRates(float(input_rate), float(output_rate)) if input_rate is not None and output_rate is not None else None
    return backend, config.max_tokens, rates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corecoder knowledge", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Build an explicit, versioned repository index")
    common_arguments(index)
    index.add_argument("--mode", choices=["lexical", "vector", "hybrid"],
                       help="lexical builds a local keyword index without embeddings or model requests")
    index.add_argument("--dry-run", action="store_true", help="Preview authorized files without model calls or storage writes")
    index.add_argument("--include-article", action="store_true", default=None)
    status = commands.add_parser("status", help="Inspect the active snapshot without a Chat key")
    common_arguments(status)
    search = commands.add_parser("search", help="Search one published repository snapshot")
    common_arguments(search)
    search.add_argument("query")
    search.add_argument("--mode", choices=["lexical", "vector", "hybrid"])
    search.add_argument("--top-k", type=int)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(vars(args))
        ledger = BudgetLedger(WorkflowBudget(args.max_requests, args.max_tokens, args.deadline, args.max_cost))
        gateway = None
        index_mode = getattr(args, "mode", None) or settings.retrieval_mode
        if args.command == "index" and not args.dry_run and index_mode != "lexical":
            gateway = settings.gateway(ledger)
        elif args.command == "search" and (args.mode or settings.retrieval_mode) != "lexical":
            gateway = settings.gateway(ledger, required=False)
        with _service(settings, gateway) as service:
            if args.command == "index":
                result = ({**service.preview(), "mode": index_mode,
                           "embedding_provider": settings.embedding.provider if index_mode != "lexical" else None,
                           "embedding_model": (settings.embedding.model or None) if index_mode != "lexical" else None} if args.dry_run
                          else {**service.index(mode=index_mode).to_dict(), "budget": ledger.snapshot()})
            elif args.command == "status":
                result = service.status()
            else:
                result = service.search(args.query, mode=args.mode or settings.retrieval_mode,
                                        top_k=args.top_k if args.top_k is not None else settings.context_top_k).to_dict()
                result["budget"] = ledger.snapshot()
        _write(result, as_json=args.json)
        return 0
    except (KnowledgeError, BudgetExceeded) as exc:
        return _error(exc, as_json=args.json)
    except ValueError:
        return _error(KnowledgeError("configuration_error", "invalid knowledge configuration or request limits"), as_json=args.json)
    except Exception:  # noqa: BLE001 - CLI errors must not disclose source text or provider responses
        return _error(KnowledgeError("internal_error", "knowledge operation failed; no provider response or source text was logged"), as_json=args.json)


def ask_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corecoder ask", description="Answer a repository question using current indexed evidence")
    common_arguments(parser)
    parser.set_defaults(max_requests=12, max_tokens=100_000, deadline=120)
    parser.add_argument("question")
    parser.add_argument("--mode", choices=["lexical", "vector", "hybrid"])
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--sources", action="store_true", help="Sources are included in both human and JSON output")
    parser.add_argument("--offline-fixture", action="store_true", help="Use an extractive fixture; does not measure real model quality")
    parser.add_argument("--chat-model")
    parser.add_argument("--chat-base-url")
    parser.add_argument("--memory", action=argparse.BooleanOptionalAction, default=True,
                        help="Use valid local project memories (default: enabled)")
    parser.add_argument("--current-memory-key", action="append", default=[],
                        help="A key whose historical preference the current request overrides")
    parser.add_argument("--engine", choices=["direct", "graph"], default="direct")
    parser.add_argument("--thread-id")
    parser.add_argument("--turn-id")
    args = parser.parse_args(argv)
    from ..integrations.langchain_bridge import StructuredOutputError
    from ..integrations.model_gateway import GatewayError, ModelGateway
    from ..memory.models import MemoryError
    from ..workflows.claims import ClaimBusy
    from ..workflows.state import WorkflowError
    from .answering import RepositoryAnswerer

    try:
        if args.engine == "graph":
            from ..workflows.cli import ask_graph

            return ask_graph(args)
        options = vars(args).copy()
        if args.offline_fixture:
            options["provider"] = "fixture"
        settings = load_settings(options)
        ledger = BudgetLedger(WorkflowBudget(args.max_requests, args.max_tokens, args.deadline, args.max_cost))
        mode = args.mode or settings.retrieval_mode
        embeddings = settings.gateway(ledger, required=False) if mode != "lexical" else None
        backend, output_limit, rates = chat_backend(settings, args)
        gateway = ModelGateway(backend, ledger, max_output_tokens=output_limit, rates=rates)
        memory_service = None
        if args.memory:
            from ..memory.cli import service_for_root

            memory_service = service_for_root(settings.root)
        with _service(settings, embeddings) as service:
            result = RepositoryAnswerer(service, gateway, mode=args.mode or settings.retrieval_mode,
                                        top_k=args.top_k if args.top_k is not None else settings.context_top_k,
                                        context_token_budget=settings.context_token_budget, memory_service=memory_service).ask(
                                            args.question, current_memory_keys=tuple(args.current_memory_key))
        _write(result.to_dict(), as_json=args.json)
        return 0
    except (KnowledgeError, BudgetExceeded, WorkflowError) as exc:
        return _error(exc, as_json=args.json)
    except ClaimBusy:
        return _error(KnowledgeError("needs_action", "workflow is owned by another local process"), as_json=args.json)
    except GatewayError as exc:
        return _error(KnowledgeError("generation_unavailable", "Chat request failed: " + exc.category.value), as_json=args.json)
    except StructuredOutputError:
        return _error(KnowledgeError("invalid_model_output", "answer could not be validated after its bounded repair"), as_json=args.json)
    except MemoryError as exc:
        return _error(KnowledgeError("memory_unavailable", "project memory could not be validated: " + exc.category), as_json=args.json)
    except ImportError:
        return _error(KnowledgeError("configuration_error", 'repository answers require corecoder[knowledge] on Python 3.11+'), as_json=args.json)
    except ValueError:
        return _error(KnowledgeError("configuration_error", "invalid answer configuration or request limits"), as_json=args.json)
    except Exception:  # noqa: BLE001 - untrusted model/provider text must not appear in error output
        return _error(KnowledgeError("internal_error", "repository question could not be completed"), as_json=args.json)
