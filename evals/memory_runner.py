"""Compare memory off/on over frozen, deterministic cross-session histories."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from corecoder.knowledge.models import digest, relative_path
from corecoder.knowledge.scope import create_scope
from corecoder.llm import LLMResponse
from corecoder.memory.models import MemoryError, SourceAuthority
from corecoder.memory.service import MemoryService
from corecoder.memory.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/cases/memory/sequences.json"


class _Clock:
    time = datetime(2026, 11, 17, tzinfo=timezone.utc)

    def __call__(self):
        return self.time


class _CandidateBackend:
    model = "memory-extraction-fixture"

    def __init__(self, step):
        self.step = step

    def chat_once(self, messages, **kwargs):
        data = json.loads(messages[-1]["content"])["input_data"]
        return LLMResponse(content=json.dumps({"candidates": [{"kind": "preference", "key": self.step["key"],
            "content": self.step["content"], "source_ref": data["source_ref"], "evidence_ids": []}]}))


def _sequence(case: dict, directory: Path, *, enabled: bool) -> dict:
    clock, receipts, rows = _Clock(), {}, []
    project = "primary"
    store = MemoryStore(directory / "shared-memory.db")

    def session(name):
        root = directory / name
        root.mkdir(exist_ok=True)
        return MemoryService(create_scope(root), store, clock=clock)

    service = session(project)
    tp = fp = fn = deleted_residual = scope_returns = 0
    for index, step in enumerate(case["steps"]):
        action = step["action"]
        try:
            if action == "session":
                project = step.get("project", project)
                service = session(project)
            elif action == "remember":
                record = service.remember(step["content"], key=step["key"], operation_id=step["operation_id"],
                                          ttl_seconds=step.get("ttl_seconds"))
                receipts[step["operation_id"]] = record.memory_id
            elif action == "forget":
                service.forget(receipts[step["operation_id"]])
            elif action == "advance":
                clock.time += timedelta(seconds=step["seconds"])
            elif action == "file":
                target = service.scope.root / relative_path(step["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(step["text"], encoding="utf-8")
            elif action == "remove_file":
                (service.scope.root / relative_path(step["path"])).unlink()
            elif action == "rule":
                record = service.remember_rule(step["content"], path=step["path"], key=step["key"], operation_id=step["operation_id"])
                receipts[step["operation_id"]] = record.memory_id
            elif action == "unverified_run":
                from corecoder import Agent, RunStore
                from corecoder.demo import ScriptedLLM

                runs = RunStore(service.scope.root / "runs.db")
                result = Agent(llm=ScriptedLLM([LLMResponse(content=step["text"])]), tools=[], run_store=runs).run("Check project")
                service.run_store = runs
                try:
                    service.run_source(result.run_id)
                except MemoryError as exc:
                    assert exc.category == "unverified_source"
                else:
                    raise AssertionError("unverified run was accepted")
            elif action == "forged_run":
                forged = SourceAuthority("run", "model-claimed-verified", digest("verified"), ("verify:forged",))
                try:
                    service._write("experience", "task.result", "Unverified claim", forged, "forged", None, "active")
                except MemoryError as exc:
                    assert exc.category == "unverified_source"
                else:
                    raise AssertionError("forged run was accepted")
            elif action == "extract":
                from corecoder.integrations.model_gateway import ModelGateway

                source_text = "Remember: " + step["content"]
                source = service.explicit_source(source_text, step["operation_id"])
                record, = service.stage_extraction(ModelGateway(_CandidateBackend(step)), source_text=source_text,
                                                   source=source, operation_id=step["operation_id"])
                receipts[step["operation_id"]] = record.memory_id
                assert record.status == "candidate"
            elif action == "approve":
                service.approve(receipts[step["operation_id"]])
            elif action == "recall":
                records = (service.recall(step["query"], current_keys=tuple(step.get("current_keys", [])),
                                          token_budget=step.get("token_budget", 1000)).memories if enabled else ())
                actual = {record.key: record.content for record in records}
                expected = step["expect"]
                matches = sum(actual.get(key) == value for key, value in expected.items())
                tp += matches
                fp += len(actual) - matches
                fn += len(expected) - matches
                deleted_residual += len(records) if step.get("deleted_probe") else 0
                scope_returns += len(records) if step.get("scope_probe") else 0
                rows.append({"step": index, "expected_keys": sorted(expected), "actual_keys": sorted(actual),
                             "exact_match": actual == expected})
            else:
                raise ValueError("unknown memory sequence action")
        except MemoryError as exc:
            if exc.category != step.get("expected_error"):
                raise
            rows.append({"step": index, "expected_rejection": exc.category})
        else:
            if "expected_error" in step:
                raise AssertionError("expected memory rejection did not occur")
    return {"id": case["id"], "category": case["category"], "true_positive": tp, "false_positive": fp,
            "false_negative": fn, "deleted_residual": deleted_residual, "scope_returns": scope_returns, "steps": rows}


def run_evaluation() -> dict:
    dataset = json.loads(CASES.read_text(encoding="utf-8"))
    assert len(dataset["cases"]) == 20 and len({case["id"] for case in dataset["cases"]}) == 20
    variants = {}
    with tempfile.TemporaryDirectory(prefix="corecoder-memory-eval-") as temporary:
        for name, enabled in (("off", False), ("on", True)):
            rows = []
            for case in dataset["cases"]:
                directory = Path(temporary) / name / case["id"]
                directory.mkdir(parents=True)
                rows.append(_sequence(case, directory, enabled=enabled))
            tp, fp, fn = (sum(row[key] for row in rows) for key in ("true_positive", "false_positive", "false_negative"))
            checks = [step for row in rows for step in row["steps"] if "exact_match" in step]
            variants[name] = {"true_positive": tp, "false_positive": fp, "false_negative": fn,
                              "recall_precision": tp / (tp + fp) if tp + fp else None,
                              "necessary_constraint_recall": tp / (tp + fn) if tp + fn else None,
                              "exact_match_count": sum(step["exact_match"] for step in checks), "recall_checks": len(checks),
                              "deleted_residual": sum(row["deleted_residual"] for row in rows),
                              "scope_returns": sum(row["scope_returns"] for row in rows), "cases": rows}
    sources = sorted((ROOT / "corecoder/memory").glob("*.py")) + [Path(__file__), CASES]
    return {"mode": "offline_fixture", "python": platform.python_version(), "platform": platform.system(),
            "sequence_count": 20, "dataset_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
            "source_manifest": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
            "limitations": "Deterministic lifecycle and prompt-context selection only; live model constraint adherence is not evaluated.",
            "results": variants}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/results/next-stage/memory-fixture.json")
    args = parser.parse_args(argv)
    report = run_evaluation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Memory lifecycle comparison", "", report["limitations"], "",
             "| Memory | Context checks | Necessary recall | Precision | Deleted residual | Scope returns |",
             "|---|---:|---:|---:|---:|---:|"]
    for mode, result in report["results"].items():
        lines.append(f"| {mode} | {result['exact_match_count']}/{result['recall_checks']} | {result['necessary_constraint_recall']} | "
                     f"{result['recall_precision']} | {result['deleted_residual']} | {result['scope_returns']} |")
    args.output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    result = report["results"]["on"]
    return int(result["false_positive"] != 0 or result["false_negative"] != 0
               or result["deleted_residual"] != 0 or result["scope_returns"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
