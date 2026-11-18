"""Stable identities, durable budgets and scoped payload recovery boundaries."""

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from corecoder import Agent, FileStateVerifier, RunStatus, RunStore
from corecoder.budget import CancellationToken, RunStopped
from corecoder.context import ContextManager
from corecoder.demo import ScriptedLLM
from corecoder.integrations.agent_llm_bridge import AgentLLMBridge
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.models import digest
from corecoder.knowledge.scope import create_scope
from corecoder.llm import LLMResponse, ToolCall
from corecoder.run_store import InjectedProcessCrash, RunStoreError
from corecoder.tools.write import WriteFileTool
from corecoder.workflows.budget_ledger import BudgetExceeded, TokenRates, WorkflowBudget
from corecoder.workflows.claims import ClaimBusy, exclusive_file_claim
from corecoder.workflows.durable_budget import DurableBudgetLedger
from corecoder.workflows.payload_store import PayloadStore
from corecoder.workflows.state import WorkflowError


def test_stable_run_reuses_terminal_result_and_rejects_different_input(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    artifact = tmp_path / "ready.md"
    artifact.write_text("ready", encoding="utf-8")
    task = {"goal": "Check artifact", "verifier": FileStateVerifier(artifact, content_equals="ready")}
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[], run_store=store)
    config = digest(["file-check", "ready"])
    first = agent.run_once(task, run_id="stable-child", configuration_hash=config)
    assert first.status == RunStatus.VERIFIED
    assert agent.run_once(task, run_id="stable-child", configuration_hash=config).to_dict() == replace(first, metrics={}).to_dict()
    fresh = Agent(llm=ScriptedLLM([]), tools=[], run_store=store)
    assert fresh.run_once(task, run_id="stable-child", configuration_hash=config).status == RunStatus.VERIFIED
    with pytest.raises(RunStoreError, match="different task"):
        fresh.run_once("different input", run_id="stable-child", configuration_hash=config)
    with pytest.raises(RunStoreError, match="different task"):
        fresh.run_once(task, run_id="stable-child", configuration_hash=digest("changed verifier"))


def test_stable_mapping_survives_crash_before_first_run_record(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    original = Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[], run_store=store)

    def crash(*args, **kwargs):
        raise InjectedProcessCrash("after durable identity, before child record")

    original.run = crash
    with pytest.raises(InjectedProcessCrash):
        original.run_once("task", run_id="mapped-child", configuration_hash=digest("config"))
    fresh = Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[], run_store=store)
    result = fresh.run_once("task", run_id="mapped-child", configuration_hash=digest("config"))
    assert result.run_id == "mapped-child" and len(store.list_runs()) == 1


def test_claim_excludes_a_real_second_process_and_releases(tmp_path):
    lock = tmp_path / "operation.lock"
    code = "from pathlib import Path; from corecoder.workflows.claims import exclusive_file_claim; " \
           "claim=exclusive_file_claim(Path(__import__('sys').argv[1])); claim.__enter__()"
    with exclusive_file_claim(lock):
        result = subprocess.run([sys.executable, "-c", code, str(lock)], check=False, capture_output=True, text=True, timeout=15)
        assert result.returncode != 0 and "ClaimBusy" in result.stderr
        with pytest.raises(ClaimBusy), exclusive_file_claim(lock):
            pytest.fail("concurrent claim was admitted")
    with exclusive_file_claim(lock):
        pass


def test_durable_budget_retains_reservations_unknown_usage_and_deadline(tmp_path):
    now = [100.0]
    budget = WorkflowBudget(max_requests=2, max_tokens=1000, deadline_seconds=10)
    first = DurableBudgetLedger(tmp_path / "budget.db", "turn", budget, clock=lambda: now[0])
    first.reserve(100, 20)
    second = DurableBudgetLedger(first.path, "turn", budget, clock=lambda: now[0])
    assert second.snapshot()["requests"] == 1 and second.snapshot()["pending_requests"] == 1
    assert second.recover_pending() == 1 and second.recover_pending() == 0
    assert second.snapshot()["tokens"] == 120 and second.snapshot()["cost_usd"] is None
    assert second.snapshot()["unknown_usage_requests"] == 1
    reservation = second.reserve(10, 5)
    second.settle(reservation, prompt_tokens=8, completion_tokens=3)
    second.settle(reservation, prompt_tokens=8, completion_tokens=3)
    assert second.snapshot()["tokens"] == 131
    with pytest.raises(BudgetExceeded, match="request"):
        second.reserve(1, 1)
    now[0] = 110
    assert DurableBudgetLedger(first.path, "turn", clock=lambda: now[0]).remaining_seconds() == 0
    with pytest.raises(BudgetExceeded, match="deadline"):
        second.check()
    with pytest.raises(WorkflowError, match="cannot change"):
        DurableBudgetLedger(first.path, "turn", WorkflowBudget(max_requests=99))


def test_durable_cost_and_reservation_scope(tmp_path):
    rates = TokenRates(1_000_000, 2_000_000)
    budget = WorkflowBudget(max_requests=3, max_tokens=100, max_cost_usd=20)
    ledger = DurableBudgetLedger(tmp_path / "budget.db", "one", budget)
    reservation = ledger.reserve(4, 2, rates)
    with pytest.raises(ValueError, match="pricing"):
        ledger.settle(reservation, prompt_tokens=1, completion_tokens=1)
    ledger.settle(reservation, prompt_tokens=3, completion_tokens=1, rates=rates)
    assert ledger.snapshot()["cost_usd"] == 5
    other = DurableBudgetLedger(tmp_path / "other.db", "one", budget)
    other.reserve(4, 2, rates)
    with pytest.raises(ValueError, match="different durable ledger"):
        other.settle(reservation, rates=rates)
    with pytest.raises(BudgetExceeded, match="cost limit"):
        ledger.reserve(10, 4, rates)


def test_payload_scope_purpose_integrity_expiry_and_sensitive_input(tmp_path):
    now = [100.0]
    scope = create_scope(tmp_path)
    store = PayloadStore(tmp_path / "payloads.db", clock=lambda: now[0])
    ref = store.put(scope, "request", {"question": "Explain RunStore"}, ttl_seconds=10)
    assert store.get(scope, ref, "request") == {"question": "Explain RunStore"}
    with pytest.raises(WorkflowError, match="missing"):
        store.get(scope, ref, "plan")
    with pytest.raises(WorkflowError, match="outside"):
        store.get(replace(scope, project_id="another-project"), ref, "request")
    with pytest.raises(WorkflowError, match="credential"):
        store.put(scope, "request", {"question": "api_key=sk-TEST_SECRET_123456"})
    with store.transaction() as db:
        db.execute("UPDATE payloads SET body=? WHERE ref=?", (json.dumps({"question": "changed"}), ref))
    with pytest.raises(WorkflowError, match="hash"):
        store.get(scope, ref, "request")
    other_ref = store.put(scope, "request", {"question": "Different"}, ttl_seconds=10)
    now[0] = 110
    with pytest.raises(WorkflowError, match="expired"):
        store.get(scope, other_ref, "request")


class Backend:
    model = "budgeted-agent-fixture"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat_once(self, messages, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_child_agent_and_summary_share_durable_budget(tmp_path):
    ledger = DurableBudgetLedger(tmp_path / "budget.db", "turn", WorkflowBudget(max_requests=1, max_tokens=100_000))
    backend = Backend([LLMResponse(content="summary")])
    gateway = ModelGateway(backend, ledger)
    bridge = AgentLLMBridge(gateway)
    assert ContextManager()._get_summary([{"role": "user", "content": "Earlier details"}], bridge) == "summary"
    result = Agent(llm=bridge, tools=[], run_store=RunStore(tmp_path / "runs.db")).run("new coding task")
    assert result.exit_reason.value == "workflow_budget_exceeded" and backend.calls == 1
    assert gateway.records[0].operation == "summarize"
    assert DurableBudgetLedger(ledger.path, "turn").snapshot()["requests"] == 1


def test_cancelled_model_adapter_cannot_start_a_request_or_tool(tmp_path):
    token = CancellationToken()
    token.cancel()
    backend = Backend([LLMResponse(tool_calls=[ToolCall("w", "write_file", {"file_path": str(tmp_path / "bad.md"), "content": "bad"})])])
    ledger = DurableBudgetLedger(tmp_path / "budget.db", "turn", cancellation_token=token)
    bridge = AgentLLMBridge(ModelGateway(backend, ledger), cancellation_token=token)
    result = Agent(llm=bridge, tools=[WriteFileTool()]).run("write a file", cancellation_token=token)
    assert result.status == RunStatus.CANCELLED and backend.calls == 0 and not (tmp_path / "bad.md").exists()
    with pytest.raises(RunStopped):
        bridge.chat([{"role": "user", "content": "hello"}])
