"""Matched direct-runtime/graph recovery with the same tool, verifier and faults.

Both variants use the public stable run identity and the same durable gateway
ledger. This measures orchestration overhead and contracts, not model quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import tempfile
import time
from pathlib import Path

from corecoder import Agent, FileStateVerifier, RunStore
from corecoder.integrations.agent_llm_bridge import AgentLLMBridge
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import FixtureEmbeddingProvider
from corecoder.knowledge.models import digest
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.permissions import Permission
from corecoder.run_store import InjectedProcessCrash
from corecoder.tools.write import WriteFileTool
from corecoder.workflows.budget_ledger import WorkflowBudget
from corecoder.workflows.durable_budget import DurableBudgetLedger
from corecoder.workflows.graph import GraphWorkflow
from examples.graph_resume_demo import RecoveryBackend

ROOT = Path(__file__).resolve().parent.parent
FAULTS = ("tool_after_execute_before_receipt", "tool_after_result_persisted_before_receipt",
          "verification_after_execute_before_result")


def run_case(root: Path, variant: str, boundary: str) -> dict:
    root.mkdir()
    scope, target = create_scope(root), root / "artifact.md"
    store = RunStore(root / ".corecoder/runs.db")
    verifier = FileStateVerifier(target, content_equals="ready")
    writes, ledgers = [], []

    class CountedWrite(WriteFileTool):
        def execute(self, **kwargs):
            writes.append(1)
            return super().execute(**kwargs)

    def fault(name, metadata):
        if name == boundary:
            raise InjectedProcessCrash(boundary)

    def agent(gateway, crash=None):
        return Agent(AgentLLMBridge(gateway), tools=[CountedWrite()], permission=Permission(allow_all=True),
                     run_store=store, crash_injector=crash)

    def build(*, first):
        backend = RecoveryBackend(target, request_write=first)
        if variant == "direct_runtime":
            ledger = DurableBudgetLedger(root / ".corecoder/direct-budget.db", "matched", WorkflowBudget(),
                                        require_existing=not first)
            ledgers.append(ledger)
            worker = agent(ModelGateway(backend, ledger), fault if first else None)

            def invoke():
                return worker.run_once({"goal": "Create artifact.md", "verifier": verifier},
                    run_id="matched-child", configuration_hash=digest({"policy": "artifact-v1"}))

            return invoke, lambda: ledger.snapshot()
        workflow = GraphWorkflow(scope, gateway_factory=lambda ledger: ModelGateway(backend, ledger),
            knowledge_factory=lambda ledger: KnowledgeService(scope, EmbeddingGateway(FixtureEmbeddingProvider(64), ledger)),
            agent_factory=lambda gateway, cancellation: agent(gateway, fault if first else None),
            trusted_configuration={"comparison": "recovery-v1"}, policy_id="artifact-v1", verifier=verifier)
        workflow_id = workflow.workflow_id("matched", "turn")
        invoke = (lambda: workflow.run("Create artifact.md", intent="code", thread_id="matched", turn_id="turn", mode="lexical")) if first else (
                  lambda: workflow.resume(workflow_id))
        return invoke, lambda: workflow.status(workflow_id).budget

    started = time.monotonic()
    invoke, budget = build(first=True)
    try:
        invoke()
    except InjectedProcessCrash:
        pass
    else:
        raise AssertionError("specified fault did not occur")
    before = budget()
    child_id = store.list_runs()[0]["run_id"]
    invoke, budget = build(first=False)
    reopened = budget() if variant == "direct_runtime" else before
    result = invoke()
    status = result.status.value if variant == "direct_runtime" else result.code_run_status
    after = budget()
    invoke()  # Terminal reuse must not add tools or requests.
    terminal = budget()
    assert status == "verified" and writes == [1] and len(store.list_runs()) == 1
    assert store.list_runs()[0]["run_id"] == child_id
    assert reopened["requests"] == before["requests"] and after["requests"] >= before["requests"]
    assert terminal == after
    return {"variant": variant, "fault": boundary, "status": status, "physical_writes": len(writes),
            "duplicate_effects": len(writes) - 1, "same_child": True, "budget_before_resume": before,
            "budget_after_resume": after, "terminal_requests_added": terminal["requests"] - after["requests"],
            "elapsed_ms": (time.monotonic() - started) * 1000}


def run_comparison() -> dict:
    with tempfile.TemporaryDirectory(prefix="corecoder-recovery-comparison-") as directory:
        rows = [run_case(Path(directory) / f"{variant}-{index}", variant, boundary)
                for index, boundary in enumerate(FAULTS) for variant in ("direct_runtime", "graph")]
    paths = sorted((ROOT / "corecoder").rglob("*.py")) + [Path(__file__), ROOT / "examples/graph_resume_demo.py"]
    return {"mode": "offline_fixture", "python": platform.python_version(), "faults": list(FAULTS), "runs_per_case": 1,
            "fixed": "RecoveryBackend, WriteFileTool, file-state verifier, stable identity, durable gateway budget",
            "limitations": "Three in-process injected crash boundaries; not OS kill/power loss, arbitrary tools or live-model performance. Both variants use durable budget integration. Graph adds plan/commit stages.",
            "source_manifest": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
            "rows": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/results/next-stage/recovery-comparison.json")
    args = parser.parse_args(argv)
    report = run_comparison()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "runs": len(report["rows"]),
                      "duplicate_effects": sum(row["duplicate_effects"] for row in report["rows"]),
                      "verified": sum(row["status"] == "verified" for row in report["rows"]),
                      "limitations": report["limitations"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
