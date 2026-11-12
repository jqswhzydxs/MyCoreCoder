"""Acceptance coverage for the offline reliability demo."""

from corecoder import RunStatus
from examples.reliablecoder_demo import FIXED_SOURCE, run_demo


def test_reliability_demo_uses_real_evidence_and_exactly_once_resume(tmp_path):
    workspace = tmp_path / "demo"

    outcome = run_demo(workspace, show_trace=False)

    assert outcome.status is RunStatus.VERIFIED
    assert outcome.edit_executions == 1
    assert outcome.test_exit_code == 0
    assert (workspace / "calculator.py").read_text(encoding="utf-8") == FIXED_SOURCE
    assert outcome.trace_file.is_file()
    assert "verification_finished" in outcome.event_types
    assert "run_finished" in outcome.event_types
