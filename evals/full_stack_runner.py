"""Unified offline acceptance, plus an explicitly enabled bounded live API probe.

python -m evals.full_stack_runner
python -m evals.full_stack_runner --live-api
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from corecoder.integrations.langchain_bridge import StructuredPromptChain
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.cli import chat_backend
from corecoder.knowledge.settings import load_settings
from corecoder.workflows.budget_ledger import BudgetLedger, WorkflowBudget
from evals.prompt_runner import run_comparison
from examples.full_stack_demo import require, run_demo
from examples.graph_resume_demo import main as recovery_demo
from examples.knowledge_mcp_demo import run_demo as mcp_agent_demo

ROOT = Path(__file__).resolve().parents[1]


def prompt_contracts():
    report = run_comparison(["v1", "v2"])
    results = {key: {"passed": value["passed"], "total": value["total"]} for key, value in report["results"].items()}
    require(all(row["passed"] == row["total"] for row in results.values()), "Prompt contracts failed")
    return {"versions": results, "prompt_hashes": report["prompt_hashes"], "mode": report["mode"]}


def recovery():
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        require(recovery_demo() == 0, "coding recovery demo failed")
    report = json.loads(captured.getvalue())
    require(report["code_run_status"] == "verified" and report["physical_writes"] == 1
            and report["terminal_resume_requests_added"] == 0, "coding recovery invariants failed")
    return report


def live_api_probe():
    """Small fixed inputs only; never send the working repository or memory DB."""
    from corecoder.config import Config

    # Credentials are read only after the explicit --live-api branch.
    Config.from_env()
    settings = load_settings({"root": str(ROOT)})
    limits = WorkflowBudget(max_requests=4, max_tokens=30_000, deadline_seconds=60)
    ledger = BudgetLedger(limits)
    checks, backend = {}, None
    try:
        backend, output_limit, rates = chat_backend(settings,
            SimpleNamespace(offline_fixture=False, chat_model=None, chat_base_url=None))
        gateway = ModelGateway(backend, ledger, max_attempts=1, max_output_tokens=min(output_limit, 512), rates=rates)
        result = StructuredPromptChain(gateway, "repo_answer.v2").invoke({
            "question": "What does RunStore persist?",
            "evidence": [{"id": "api-probe-evidence", "text": "RunStore persists tool execution state before recovery."}],
            "memories": [],
        })
        require(result.value.status == "answered" and result.value.evidence_ids == ["api-probe-evidence"],
                "API probe did not return an evidence-bound structured answer")
        checks["chat"] = {"status": "passed", "model": backend.model, "prompt_id": result.template_id, "schema_valid": True}
    except Exception as exc:  # noqa: BLE001 - sanitize provider failures at this opt-in report boundary
        checks["chat"] = {"status": "failed", "error_category": failure_category(exc)}
    finally:
        client = getattr(backend, "client", None)
        if client is not None:
            client.close()
    if not settings.embedding.model or not os.getenv(settings.embedding.api_key_env):
        checks["embedding"] = {"status": "not_configured", "model": settings.embedding.model,
                               "error_category": "missing_independent_embedding_configuration"}
    else:
        try:
            embeddings = settings.gateway(ledger)
            require(embeddings.space.provider != "fixture", "live probe requires a real embedding provider")
            embeddings.max_attempts = 1
            vector = embeddings.embed_query("RunStore persists tool execution state before recovery.")
            checks["embedding"] = {"status": "passed", "model": settings.embedding.model, "dimensions": len(vector)}
        except Exception as exc:  # noqa: BLE001 - sanitize provider failures at this opt-in report boundary
            checks["embedding"] = {"status": "failed", "error_category": failure_category(exc)}
    status = ("failed" if any(row["status"] == "failed" for row in checks.values()) else
              "passed" if all(row["status"] == "passed" for row in checks.values()) else "incomplete")
    return {"mode": "live_api_connectivity", "status": status, "checks": checks,
        "request_limit": limits.max_requests, "budget": ledger.snapshot(),
        "limits": "Tiny synthetic-input connectivity/schema probe; not full-corpus Recall or human-reviewed answer quality."}


def failure_category(exc):
    category = getattr(exc, "category", None)
    return getattr(category, "value", None) or type(exc).__name__


def run_acceptance(*, live_api=False):
    stages = [("full_stack", run_demo), ("prompt_contracts", prompt_contracts),
              ("agent_mcp_sources", mcp_agent_demo), ("coding_crash_resume", recovery)]
    if live_api:
        stages.append(("live_api", live_api_probe))
    results = {}
    for name, invoke in stages:
        started = time.monotonic()
        try:
            detail = invoke()
            results[name] = {"status": detail.get("status", "passed"), "detail": detail}
        except Exception as exc:  # noqa: BLE001 - isolate and sanitize independent acceptance failures
            # Provider messages, requests, local paths and credentials are not report data.
            results[name] = {"status": "failed", "error_category": failure_category(exc)}
        results[name]["elapsed_seconds"] = round(time.monotonic() - started, 3)
    files = sorted((ROOT / "corecoder").rglob("*.py")) + sorted((ROOT / "corecoder/prompts").glob("*.txt"))
    files += [Path(__file__), ROOT / "examples/full_stack_demo.py", ROOT / "examples/graph_resume_demo.py",
              ROOT / "examples/knowledge_mcp_demo.py", ROOT / "evals/prompt_runner.py", ROOT / "pyproject.toml"]
    return {"schema_version": 1, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "platform": platform.system(),
        "status": ("failed" if any(row["status"] == "failed" for row in results.values()) else
                   "passed" if all(row["status"] == "passed" for row in results.values()) else "incomplete"),
        "live_api_requested": live_api, "stages": results,
        "dependencies": {name: version(name) for name in ("openai", "langchain-core", "langgraph", "chromadb", "mcp")},
        "source_manifest": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
        "limits": "Offline acceptance uses scripted models/vectors; live API probe is separate. Human quality review is pending."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-api", action="store_true", help="Enable a paid synthetic-input Chat/Embedding probe (4 requests/60s max)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_acceptance(live_api=args.live_api)
    output = args.output or ROOT / (".corecoder/acceptance/live.json" if args.live_api else "evals/results/full-stack/acceptance.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output),
                      "stages": {name: row["status"] for name, row in report["stages"].items()}}, ensure_ascii=False))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
