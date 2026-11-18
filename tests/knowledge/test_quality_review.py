"""Human quality gates cannot be satisfied by fixtures or missing judgments."""

from copy import deepcopy

import pytest

from corecoder.knowledge.models import digest
from evals.quality_runner import review_report


def fixture():
    answers, labels = [], []
    for template in ("repo_answer.v1", "repo_answer.v2"):
        for enabled in (False, True):
            for number, answerable in enumerate((True, False)):
                key = f"{template}:{enabled}:{number}"
                answers.append({"key": key, "template": template, "retrieval": enabled,
                                "expected_answerable": answerable, "schema_valid": True,
                                "status": "answered" if answerable and enabled else "insufficient"})
                labels.append({"key": key, "supported": True, "correct": True, "refusal_correct": True})
    report = {"mode": "live", "failure": None, "answers": answers,
              "retrieval": [{"mode": "hybrid", "gold_total": 2, "gold_hits": 2}]}
    return report, {"report_sha256": digest(report), "human_review": True,
                    "reviewer": "test-only human-label fixture", "labels": labels}


def test_review_requires_real_mode_all_labels_and_valid_schemas():
    report, labels = fixture()
    assert review_report(report, labels)["quality_gate_passed"]
    report["mode"] = "offline_fixture"
    labels["report_sha256"] = digest(report)
    assert not review_report(report, labels)["quality_gate_passed"]
    report["mode"] = "live"
    labels["report_sha256"] = digest(report)
    missing = deepcopy(labels)
    missing["labels"].pop()
    assert not review_report(report, missing)["quality_gate_passed"]
    report["answers"][0]["schema_valid"] = False
    labels["report_sha256"] = digest(report)
    assert not review_report(report, labels)["quality_gate_passed"]


def test_review_rejects_duplicate_unknown_and_nonboolean_labels():
    report, labels = fixture()
    for field, value in (("key", "unknown"), ("supported", "true")):
        invalid = deepcopy(labels)
        invalid["labels"][0][field] = value
        with pytest.raises(ValueError):
            review_report(report, invalid)
    labels["labels"].append(labels["labels"][0])
    with pytest.raises(ValueError):
        review_report(report, labels)


def test_review_reports_real_denominators_and_fails_quality_targets():
    report, labels = fixture()
    report["retrieval"][0]["gold_hits"] = 1
    labels["report_sha256"] = digest(report)
    labels["labels"][-1]["refusal_correct"] = False
    result = review_report(report, labels)
    assert result["hybrid_recall_at_5_macro"] == .5
    assert result["groups"]["repo_answer.v2:rag-1"]["correct_no_answer"] == 0
    assert not result["quality_gate_passed"]


def test_old_labels_cannot_validate_a_changed_report():
    report, labels = fixture()
    report["answers"][0]["answer"] = "Changed after human review"
    with pytest.raises(ValueError, match="exact saved experiment"):
        review_report(report, labels)
