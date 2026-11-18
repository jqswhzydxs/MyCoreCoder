"""Compare versioned Prompt plumbing on identical deterministic fixtures.

This is a contract regression, not a model quality or semantic support test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path

from corecoder.integrations.langchain_bridge import StructuredOutputError, StructuredPromptChain
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.llm import LLMResponse
from corecoder.prompts.registry import load_prompt

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals/cases/prompts/contracts.json"


class FixtureBackend:
    model = "fixed-structured-responses"

    def __init__(self, responses: list):
        self._responses = iter(responses)

    def chat_once(self, messages, *, tools, timeout_seconds, max_tokens):
        value = next(self._responses)
        return LLMResponse(content=value if isinstance(value, str) else json.dumps(value))


def run_comparison(versions: list[str]) -> dict:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    results = {}
    for prompt_version in versions:
        rows = []
        for case in cases:
            gateway = ModelGateway(FixtureBackend(case["responses"]))
            chain = StructuredPromptChain(gateway, f"{case['kind']}.{prompt_version}")
            repaired = False
            try:
                result = chain.invoke(case["values"])
                outcome = "accepted"
                repaired = result.repaired
            except StructuredOutputError:
                outcome = "rejected"
            rows.append({
                "id": case["id"], "outcome": outcome, "expected": case["expected"],
                "passed": outcome == case["expected"], "repaired": repaired,
                "requests": gateway.ledger.snapshot()["requests"],
            })
        results[prompt_version] = {"passed": sum(row["passed"] for row in rows), "total": len(rows), "cases": rows}
    source_files = sorted((ROOT / "corecoder").rglob("*.py")) + sorted((ROOT / "corecoder/prompts").glob("*.txt"))
    source_files.extend([CASES, Path(__file__), ROOT / "pyproject.toml"])
    source_manifest = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    return {
        "mode": "deterministic_offline_contracts",
        "limitations": "Identical scripted responses for both versions; this does not measure live model quality, semantic support, or retrieval.",
        "python": platform.python_version(), "platform": platform.system(),
        "dependencies": {name: version(name) for name in ("langchain-core", "pydantic")},
        "case_manifest_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
        "source_manifest": source_manifest,
        "prompt_hashes": {
            f"{kind}.{v}": load_prompt(f"{kind}.{v}").sha256
            for kind in ("repo_answer", "task_plan", "memory_extract") for v in versions
        },
        "cost_usd": None, "usage": "unknown; fixtures have no provider usage",
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", nargs="+", choices=["v1", "v2"], default=["v1", "v2"])
    parser.add_argument("--offline", action="store_true", help="Required: never load provider credentials or call a live API")
    parser.add_argument("--output", type=Path, default=ROOT / "evals/results/llm-engineering/prompts-offline.json")
    args = parser.parse_args(argv)
    if not args.offline:
        parser.error("only --offline contract evaluation is implemented")
    report = run_comparison(args.compare)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Offline Prompt contract comparison", "", report["limitations"], "", "| Version | Passed / Total |", "|---|---:|"]
    for v, result in report["results"].items():
        lines.append(f"| {v} | {result['passed']} / {result['total']} |")
    lines.extend(["", "Provider tokens and cost: unavailable. Fixture outputs are fixed across versions."])
    args.output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return int(any(result["passed"] != result["total"] for result in report["results"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
