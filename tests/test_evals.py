"""Tests for the fully offline baseline/candidate Agent eval harness."""

import copy
import json
from collections import Counter

import pytest

from evals.compare import compare_payloads
from evals.compare import markdown_text as comparison_markdown
from evals.report import json_text, markdown_text, write_reports
from evals.runner import CATEGORIES, REQUIRED_CASE_FIELDS, load_cases, run_case, run_suite


@pytest.fixture(scope="module")
def baseline_payload():
    return run_suite(variant="baseline")


@pytest.fixture(scope="module")
def candidate_payload():
    return run_suite(variant="candidate")


@pytest.fixture(scope="module")
def gates():
    from evals.compare import DEFAULT_GATES

    return json.loads(DEFAULT_GATES.read_text(encoding="utf-8"))


def test_eval_matrix_has_required_declarations_and_balanced_categories():
    cases = load_cases()
    counts = Counter(case["category"] for case in cases)

    assert len(cases) >= 40
    assert set(counts) == set(CATEGORIES)
    assert all(count >= 4 for count in counts.values())
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert REQUIRED_CASE_FIELDS <= case.keys()
        assert isinstance(case["expected_tool_calls"], list)
        assert isinstance(case["forbidden_tool_calls"], list)
        assert isinstance(case["expected_file_state"], dict)


def test_offline_eval_is_deterministic_and_writes_both_reports(
    baseline_payload,
    candidate_payload,
    tmp_path,
):
    second = run_suite(variant="baseline")

    assert baseline_payload == second
    assert candidate_payload == run_suite(variant="candidate")
    assert baseline_payload["metadata"]["model"] == {
        "type": "FaultInjectingLLM",
        "name": "scripted-eval-v2",
        "offline": True,
    }
    assert baseline_payload["metrics"]["case_pass_rate"] == 1.0
    assert baseline_payload["metrics"]["counts"]["unique_cases"] >= 40

    json_path, markdown_path = write_reports(baseline_payload, tmp_path / "baseline")
    assert json_path.read_text(encoding="utf-8") == json_text(baseline_payload)
    assert markdown_path.read_text(encoding="utf-8") == markdown_text(baseline_payload)
    assert "Git commit" in markdown_path.read_text(encoding="utf-8")


def test_eval_failure_names_case_and_reason():
    case = copy.deepcopy(load_cases()[0])
    case["expected_final_outcome"]["exact"] = "a deliberately wrong result"

    result = run_case(case)

    assert not result["passed"]
    assert result["id"] == case["id"]
    assert any("final outcome" in failure for failure in result["failures"])


def test_false_completion_and_verification_slices_remain_visible(candidate_payload):
    cases = {case["id"]: case for case in candidate_payload["cases"]}

    assert cases["false_completion_missing_evidence"]["final_outcome"][
        "false_completion"
    ]
    assert cases["false_completion_missing_evidence"]["verification"]["status"] == "failed"
    assert cases["verification_test_failure"]["verification"]["status"] == "failed"
    assert cases["verification_evidence_missing"]["verification"]["status"] == (
        "insufficient_evidence"
    )
    partial = cases["verification_partial_success"]["verification"]
    assert partial["status"] == "failed"
    assert [item["passed"] for item in partial["evidence"]] == [True, False]
    assert candidate_payload["metrics"]["false_completion_rate"] > 0
    assert candidate_payload["metrics"]["verified_completion_rate"] > 0


def test_fault_matrix_exercises_every_deterministic_fault(candidate_payload):
    cases = {case["id"]: case for case in candidate_payload["cases"]}

    assert "invalid_json_arguments:0" in cases["malformed_invalid_json_arguments"][
        "injected_faults"
    ]
    assert any("timeout" in item for item in cases["timeout_read"]["injected_faults"])
    assert cases["transient_llm_429"]["model_attempts"] == 3
    assert cases["transient_llm_5xx"]["model_attempts"] == 4
    assert cases["transient_llm_connection"]["model_attempts"] == 3
    assert cases["crash_after_tool_execute"]["crash_resumed"]
    assert "response_lost" in cases["duplicate_response_lost_create"][
        "injected_faults"
    ][0]


def test_candidate_recovers_without_duplicate_side_effects(
    baseline_payload,
    candidate_payload,
):
    baseline = baseline_payload["metrics"]
    candidate = candidate_payload["metrics"]

    assert baseline["duplicate_side_effect_rate"] > 0
    assert baseline["crash_resume_rate"] == 0
    assert candidate["duplicate_side_effect_rate"] == 0
    assert candidate["permission_bypass_rate"] == 0
    assert candidate["crash_resume_rate"] == 1


def test_matched_comparison_enforces_independent_safety_gates(
    baseline_payload,
    candidate_payload,
    gates,
    tmp_path,
):
    comparison = compare_payloads(baseline_payload, candidate_payload, gates)

    assert comparison["gate_passed"]
    assert all(item["passed"] for item in comparison["gates"])
    report = comparison_markdown(comparison)
    assert "cannot offset a failed safety gate" in report
    assert "not a comparison to a different historical commit" in report
    path = tmp_path / "comparison.md"
    path.write_text(report, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == report


def test_comparison_rejects_different_inputs(baseline_payload, candidate_payload, gates):
    mismatched = copy.deepcopy(candidate_payload)
    mismatched["metadata"]["case_manifest_sha256"] = "different"

    with pytest.raises(ValueError, match="case manifest"):
        compare_payloads(baseline_payload, mismatched, gates)


def test_safety_gate_failure_cannot_be_offset_by_performance(
    baseline_payload,
    candidate_payload,
    gates,
):
    unsafe = copy.deepcopy(candidate_payload)
    unsafe["metrics"]["permission_bypass_rate"] = 0.01
    unsafe["metrics"]["average_latency_ms"] = 0
    unsafe["metrics"]["task_success_rate"] = 1

    comparison = compare_payloads(baseline_payload, unsafe, gates)

    assert not comparison["gate_passed"]
    failed = {item["name"] for item in comparison["gates"] if not item["passed"]}
    assert "permission_bypass_rate == 0" in failed
