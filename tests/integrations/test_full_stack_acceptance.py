"""Acceptance must preserve failures and keep paid probes explicitly opt-in."""

import json
from types import SimpleNamespace

import pytest

for dependency in ("langchain_core", "chromadb", "langgraph", "mcp"):
    pytest.importorskip(dependency)

from corecoder.config import Config
from corecoder.llm import LLMResponse
from evals import full_stack_runner as runner


def stub_offline_stages(monkeypatch):
    for name in ("run_demo", "prompt_contracts", "mcp_agent_demo", "recovery"):
        monkeypatch.setattr(runner, name, lambda: {"status": "passed"})


def test_default_acceptance_never_invokes_live_probe(monkeypatch):
    stub_offline_stages(monkeypatch)

    def forbidden():
        pytest.fail("default acceptance tried a paid endpoint")

    monkeypatch.setattr(runner, "live_api_probe", forbidden)
    report = runner.run_acceptance()
    assert report["status"] == "passed" and report["live_api_requested"] is False
    assert "live_api" not in report["stages"]


def test_failed_stage_is_redacted_and_cannot_be_offset_by_other_passes(monkeypatch):
    stub_offline_stages(monkeypatch)

    def failing():
        raise RuntimeError("fixture-secret-must-not-appear-in-report")

    monkeypatch.setattr(runner, "run_demo", failing)
    report = runner.run_acceptance()
    assert report["status"] == "failed"
    assert report["stages"]["full_stack"]["error_category"] == "RuntimeError"
    assert report["stages"]["coding_crash_resume"]["status"] == "passed"
    assert "fixture-secret-must-not-appear-in-report" not in json.dumps(report)


def test_missing_embedding_key_preserves_chat_result_and_prevents_full_pass(monkeypatch):
    class ChatFixture:
        model = "api-probe-test-fixture"

        def chat_once(self, *args, **kwargs):
            return LLMResponse(content=json.dumps({"status": "answered", "answer": "Tool execution state.",
                "evidence_ids": ["api-probe-evidence"], "unknowns": []}))

    # Keep this test independent of the developer's credentials and .env.
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: Config()))
    monkeypatch.delenv("CORECODER_ACCEPTANCE_TEST_MISSING_KEY", raising=False)
    settings = SimpleNamespace(embedding=SimpleNamespace(model="test-embedding", api_key_env="CORECODER_ACCEPTANCE_TEST_MISSING_KEY"))
    monkeypatch.setattr(runner, "load_settings", lambda options: settings)
    monkeypatch.setattr(runner, "chat_backend", lambda *args: (ChatFixture(), 512, None))
    result = runner.live_api_probe()
    assert result["checks"]["chat"]["status"] == "passed"
    assert result["checks"]["embedding"]["status"] == "not_configured"
    assert result["status"] == "incomplete" and result["budget"]["requests"] == 1
    stub_offline_stages(monkeypatch)
    monkeypatch.setattr(runner, "live_api_probe", lambda: result)
    assert runner.run_acceptance(live_api=True)["status"] == "incomplete"
