"""Bounded, opt-in live retrieval/Prompt experiments and separate human labels.

Default fixture mode rehearses the pipeline without reading model credentials.
Reports contain answers and source excerpts; default output is ignored local data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import tempfile
import time
from dataclasses import asdict, replace
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from corecoder.integrations.langchain_bridge import StructuredOutputError, StructuredPromptChain
from corecoder.integrations.model_gateway import GatewayError, ModelGateway
from corecoder.knowledge.citations import build_evidence, render_sources
from corecoder.knowledge.cli import _service, chat_backend
from corecoder.knowledge.loaders import load_sources
from corecoder.knowledge.models import KnowledgeError, digest
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.settings import load_settings
from corecoder.workflows.budget_ledger import BudgetExceeded, BudgetLedger, WorkflowBudget
from evals.knowledge_runner import CASES, ROOT, _relevant, gold_span, load_cases


def run_experiment(*, live=False, split="dev", limit=4, repetitions=1, budget=None) -> dict:
    if not 1 <= limit <= 40 or not 1 <= repetitions <= 3:
        raise ValueError("limit must be 1..40 and repetitions 1..3")
    # Fixed source corpus and dataset: no user-selected arbitrary folder is
    # silently combined with repository-specific gold labels.
    cases = [case for case in load_cases(split) if case["category"] != "adversarial"][:limit]
    source = load_sources(create_scope(ROOT))
    settings = load_settings({"root": str(ROOT)} if live else {"root": str(ROOT), "provider": "fixture", "dimensions": 64})
    if live and (settings.embedding.provider != "openai" or not settings.embedding.model):
        raise KnowledgeError("configuration_error", "live evaluation requires an explicitly configured real embedding model")
    ledger = BudgetLedger(budget or WorkflowBudget(max_requests=200, max_tokens=5_000_000, deadline_seconds=600))
    backend, output_limit, rates = chat_backend(settings, SimpleNamespace(offline_fixture=not live, chat_model=None, chat_base_url=None))
    gateway = ModelGateway(backend, ledger, max_attempts=1, max_output_tokens=min(output_limit, 1024), rates=rates)
    embeddings = settings.gateway(ledger)
    rows, retrieval, failure = [], [], None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="corecoder-quality-") as directory:
        clone = Path(directory)
        for document in source.documents:
            target = clone / document.source_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(document.text, encoding="utf-8", newline="")
        local = replace(settings, root=clone, persist_directory=".corecoder/knowledge")
        with _service(local, embeddings) as service:
            index = None
            try:
                cold = time.monotonic()
                index = service.index()
                cold_ms = (time.monotonic() - cold) * 1000
                before_hot = ledger.snapshot()["requests"]
                hot = time.monotonic()
                service.index()
                hot_ms = (time.monotonic() - hot) * 1000
                hot_requests = ledger.snapshot()["requests"] - before_hot
                assert hot_requests == 0, "unchanged corpus must not request document embeddings"
                for repetition in range(repetitions):
                    for case in cases:
                        frozen = {}
                        for mode in ("lexical", "vector", "hybrid"):
                            tick = time.monotonic()
                            result = service.search(case["query"], mode=mode, top_k=10)
                            frozen[mode] = result
                            spans = [gold_span(clone, gold) for gold in case["gold"]]
                            matched = [any(_relevant(hit, gold, span) for hit in result.hits[:5])
                                       for gold, span in zip(case["gold"], spans, strict=True)]
                            rank = next((i for i, hit in enumerate(result.hits, 1) if any(
                                _relevant(hit, gold, span) for gold, span in zip(case["gold"], spans, strict=True))), None)
                            retrieval.append({"id": case["id"], "repetition": repetition, "mode": mode,
                                "gold_hits": sum(matched), "gold_total": len(matched), "first_rank": rank,
                                "elapsed_ms": (time.monotonic() - tick) * 1000})
                        bundle = build_evidence(service.scope, case["query"], frozen["hybrid"], token_budget=4000)
                        for template in ("repo_answer.v1", "repo_answer.v2"):
                            chain = StructuredPromptChain(gateway, template)
                            for use_evidence in (False, True):
                                key = f"{case['id']}:{repetition}:{template}:rag-{int(use_evidence)}"
                                tick = time.monotonic()
                                row = {"key": key, "id": case["id"], "repetition": repetition, "template": template,
                                       "prompt_sha256": chain.template.sha256, "retrieval": use_evidence,
                                       "question": case["query"], "expected_answerable": bool(case["gold"]),
                                       "snapshot_id": bundle.snapshot_id,
                                       "supplied_evidence": [item.prompt_data() for item in bundle.evidence] if use_evidence else []}
                                try:
                                    value = chain.invoke({"question": case["query"],
                                        "evidence": [item.prompt_data() for item in bundle.evidence] if use_evidence else [],
                                        "memories": []}).value
                                    row.update(value.model_dump())
                                    row["sources"] = render_sources(bundle, value.evidence_ids)
                                    row["schema_valid"] = True
                                except (StructuredOutputError, GatewayError, KnowledgeError) as exc:
                                    row.update(status="error", schema_valid=False, error_category=type(exc).__name__)
                                row["elapsed_ms"] = (time.monotonic() - tick) * 1000
                                rows.append(row)
            except (BudgetExceeded, KnowledgeError, GatewayError, StructuredOutputError) as exc:
                failure = type(exc).__name__  # Provider exception bodies are never logged.
                cold_ms = hot_ms = hot_requests = None
    paths = sorted((ROOT / "corecoder").rglob("*.py")) + sorted((ROOT / "corecoder/prompts").glob("*.txt")) + [CASES, Path(__file__)]
    return {"mode": "live" if live else "offline_fixture", "python": platform.python_version(), "split": split,
        "case_ids": [case["id"] for case in cases], "repetitions": repetitions,
        "dependencies": {name: version(name) for name in ("langchain-core", "chromadb", "openai", "pydantic")},
        "dataset_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
        "source_manifest": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        "corpus_manifest": [{key: value for key, value in asdict(doc).items() if key != "text"} for doc in source.documents],
        "embedding_space": asdict(embeddings.provider.describe()), "chat_model": backend.model,
        "snapshot_id": index.snapshot_id if index else None, "index_cold_ms": cold_ms, "index_hot_ms": hot_ms,
        "unchanged_document_requests": hot_requests, "elapsed_ms": (time.monotonic() - started) * 1000,
        "failure": failure, "budget": ledger.snapshot(), "retrieval": retrieval, "answers": rows,
        "human_review": "pending", "semantic_quality_verified": False,
        "limitations": "Evidence-off uses the same grounded Prompt and therefore instructs refusal without evidence. It measures the evidence requirement, not unconstrained closed-book QA. Fixed fixture answers cannot establish semantic quality. Gold spans are symbol/heading overlaps; human support labels remain required."}


def review_report(report: dict, labels: dict) -> dict:
    if labels.get("report_sha256") != digest(report):
        raise ValueError("human labels must bind the exact saved experiment report")
    if labels.get("human_review") is not True or not isinstance(labels.get("reviewer"), str) or not labels["reviewer"].strip():
        raise ValueError("labels require a named human reviewer and human_review=true")
    rows = {row["key"]: row for row in report["answers"]}
    supplied = labels.get("labels", [])
    if not isinstance(supplied, list) or len({row["key"] for row in supplied}) != len(supplied):
        raise ValueError("human label keys must be unique")
    for label in supplied:
        if label["key"] not in rows or any(type(label.get(field)) is not bool for field in ("supported", "correct", "refusal_correct")):
            raise ValueError("each known answer key requires three boolean human judgments")
    reviewed = {label["key"]: label for label in supplied}
    groups = {}
    for template in ("repo_answer.v1", "repo_answer.v2"):
        for enabled in (False, True):
            subset = [row for row in rows.values() if row["template"] == template and row["retrieval"] == enabled]
            answered = [row for row in subset if row.get("status") == "answered"]
            no_answer = [row for row in subset if not row["expected_answerable"]]
            supported = sum(reviewed.get(row["key"], {}).get("supported", False) for row in answered)
            correct = sum(reviewed.get(row["key"], {}).get("correct", False) for row in subset)
            refusal = sum(reviewed.get(row["key"], {}).get("refusal_correct", False) for row in no_answer)
            groups[f"{template}:rag-{int(enabled)}"] = {"supported_answers": supported, "answered_total": len(answered),
                "correct": correct, "total": len(subset), "correct_no_answer": refusal, "no_answer_total": len(no_answer)}
    hybrid = [row for row in report["retrieval"] if row["mode"] == "hybrid" and row["gold_total"]]
    recall = sum(row["gold_hits"] / row["gold_total"] for row in hybrid) / len(hybrid) if hybrid else None
    target = groups["repo_answer.v2:rag-1"]
    support = target["supported_answers"] / target["answered_total"] if target["answered_total"] else None
    refusal = target["correct_no_answer"] / target["no_answer_total"] if target["no_answer_total"] else None
    complete = bool(rows) and set(rows) == set(reviewed) and report.get("failure") is None
    schema_ok = all(row.get("schema_valid") is True for row in rows.values())
    passed = (report["mode"] == "live" and complete and schema_ok and recall is not None and recall >= .8
              and support is not None and support >= .9 and refusal is not None and refusal >= .9)
    return {"reviewer": labels["reviewer"], "report_sha256": digest(report), "human_review": True,
            "reviewed": len(reviewed), "total": len(rows),
            "complete": complete, "all_schemas_valid": schema_ok, "groups": groups, "hybrid_recall_at_5_macro": recall,
            "quality_gate_passed": passed, "scope": "Only selected cases/repetitions; not an unmeasured full-corpus claim."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable configured paid model endpoints")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--max-tokens", type=int, default=5_000_000)
    parser.add_argument("--deadline", type=float, default=600)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--review", type=Path, help="Review a saved experiment; no model calls")
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.review:
            if args.labels is None:
                parser.error("--review requires --labels")
            report = review_report(json.loads(args.review.read_text(encoding="utf-8")), json.loads(args.labels.read_text(encoding="utf-8")))
        else:
            report = run_experiment(live=args.live, split=args.split, limit=args.limit, repetitions=args.repetitions,
                budget=WorkflowBudget(args.max_requests, args.max_tokens, args.deadline, args.max_cost))
        output = args.output or ROOT / ".corecoder/quality" / ("review.json" if args.review else "live.json" if args.live else "fixture.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not args.review:
            pending = {"report_sha256": digest(report), "human_review": True, "reviewer": "",
                       "labels": [{"key": row["key"], "supported": None, "correct": None, "refusal_correct": None}
                                  for row in report["answers"]]}
            output.with_suffix(".labels.pending.json").write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"report": str(output), "mode": report.get("mode", "human_review"),
                          "failure": report.get("failure"), "quality_gate_passed": report.get("quality_gate_passed", False)}))
        return int(bool(report.get("failure")) or (args.review is not None and not report["quality_gate_passed"]))
    except (ValueError, KnowledgeError) as exc:
        print(json.dumps({"error_category": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
