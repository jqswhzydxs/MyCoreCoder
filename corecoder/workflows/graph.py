"""A persistent StateGraph around repository services and the original Agent."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..budget import CancellationToken, RunBudget
from ..integrations.langchain_bridge import StructuredOutputError, _local_tracing
from ..integrations.model_gateway import GatewayError
from ..knowledge.models import KnowledgeError, ProjectScope, digest
from ..memory.models import MemoryError
from ..memory.service import MemoryService
from ..run_store import RunStore, RunStoreError
from .budget_ledger import BudgetExceeded, WorkflowBudget
from .checkpointing import sqlite_checkpointer
from .claims import ClaimBusy, exclusive_file_claim
from .durable_budget import DurableBudgetLedger
from .nodes import WorkflowNodes
from .operation_store import WorkflowOperations
from .payload_store import PayloadStore
from .runtime_bridge import RuntimeBridge
from .state import WorkflowError, WorkflowState


@dataclass(frozen=True)
class WorkflowOutcome:
    workflow_id: str
    workflow_status: str
    answer_status: str
    code_run_status: str
    child_run_id: str
    memory_status: str
    stage: str
    error_category: str
    fixture: bool
    budget: dict
    result: dict | None

    def to_dict(self):
        from dataclasses import asdict

        return asdict(self)


class GraphWorkflow:
    def __init__(self, scope: ProjectScope, *, gateway_factory=None, knowledge_factory=None, agent_factory=None,
                 trusted_configuration: dict | None = None, policy_id: str = "", verifier=None,
                 success_criteria: list[str] | None = None, run_budget: RunBudget | None = None,
                 memory_service: MemoryService | None = None, data_root: str | Path | None = None,
                 extract_memory: bool = False, use_memory: bool = True, fault=None, runtime_fault=None):
        self.scope = scope
        self.directory = Path(data_root) if data_root else scope.root / ".corecoder/workflows"
        self.gateway_factory, self.knowledge_factory, self.agent_factory = gateway_factory, knowledge_factory, agent_factory
        self.policy_id, self.verifier = policy_id, verifier
        if verifier is not None and not policy_id:
            raise WorkflowError("configuration_error", "a trusted verifier requires a stable policy ID and configuration revision")
        self.success_criteria = list(success_criteria or [])
        self.run_budget = run_budget or RunBudget(max_rounds=50, max_tool_calls=100)
        self.extract_memory = extract_memory
        self.use_memory = use_memory
        self.configuration_hash = digest({"version": 1, "configuration": trusted_configuration or {}, "policy_id": policy_id,
                                          "success_criteria": self.success_criteria, "run_budget": self.run_budget.to_dict(),
                                          "extract_memory": extract_memory, "use_memory": use_memory})
        self.operations = WorkflowOperations(self.directory / "workflows.db")
        self.payloads = PayloadStore(self.directory / "payloads.db")
        self.run_store = RunStore(scope.root / ".corecoder/runs.db")
        self.runtime = RuntimeBridge(scope, self.directory / "runtime-operations.db", fault=runtime_fault)
        self.memory_service = memory_service or MemoryService(scope, run_store=self.run_store)
        if self.memory_service.scope.identity() != scope.identity() or self.memory_service.scope.root != scope.root:
            raise WorkflowError("configuration_error", "workflow memory must use the same trusted project scope")
        self.fault = fault

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is None:
            raise ValueError("thread and turn IDs must be bounded identifiers")
        return value

    def workflow_id(self, thread_id, turn_id):
        return digest(["workflow-v1", self.scope.identity(), self._identity(thread_id), self._identity(turn_id)])

    def run(self, goal: str, *, thread_id: str, turn_id: str, intent: str = "ask", budget: WorkflowBudget | None = None,
            mode: str = "hybrid", top_k: int = 6, constraints: list[str] | None = None,
            current_memory_keys: tuple[str, ...] = (), memory_key: str = "response.preference",
            cancellation_token: CancellationToken | None = None, on_event=None) -> WorkflowOutcome:
        if (intent not in {"ask", "code", "remember"} or not isinstance(goal, str) or not goal.strip() or len(goal) > 4000
                or mode not in {"lexical", "vector", "hybrid"} or isinstance(top_k, bool) or not 1 <= top_k <= 50):
            raise WorkflowError("configuration_error", "invalid workflow request")
        constraints = list(constraints or [])
        if len(constraints) > 16 or any(not isinstance(item, str) or len(item) > 2000 for item in constraints):
            raise WorkflowError("configuration_error", "workflow constraints exceed the supported range")
        workflow_id = self.workflow_id(thread_id, turn_id)
        request = {"goal": goal.strip(), "intent": intent, "mode": mode, "top_k": top_k, "constraints": constraints,
                   "current_memory_keys": list(current_memory_keys), "memory_key": memory_key,
                   "budget": asdict(budget or WorkflowBudget())}
        with exclusive_file_claim(self.directory / "workflow-claims" / (workflow_id + ".lock")):
            request_ref = self.payloads.put(self.scope, "request", request, lifecycle_id=workflow_id)
            operation = self.operations.bind(self.scope, workflow_id=workflow_id, thread_id=thread_id, turn_id=turn_id,
                input_hash=digest(request), configuration_hash=self.configuration_hash, request_ref=request_ref)
            return self._invoke(operation, budget, cancellation_token or CancellationToken(), on_event=on_event)

    def resume(self, workflow_id: str, *, resume_paused: bool = False, cancellation_token=None, on_event=None) -> WorkflowOutcome:
        if re.fullmatch(r"[a-f0-9]{64}", workflow_id) is None:
            raise WorkflowError("configuration_error", "workflow ID must be the generated SHA-256 identifier")
        with exclusive_file_claim(self.directory / "workflow-claims" / (digest(workflow_id) + ".resume.lock")):
            operation = self.operations.get(self.scope, workflow_id)
            # Use the same actual execution claim as run(), including when run
            # and resume are invoked concurrently from separate processes.
            with exclusive_file_claim(self.directory / "workflow-claims" / (workflow_id + ".lock")):
                if operation["configuration_hash"] != self.configuration_hash:
                    raise WorkflowError("input_mismatch", "trusted workflow configuration changed; automatic recovery is refused")
                return self._invoke(operation, None, cancellation_token or CancellationToken(),
                                    resume_paused=resume_paused, on_event=on_event)

    def _build(self, nodes, checkpointer):
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import RetryPolicy

        builder = StateGraph(WorkflowState)
        for name in ("prepare", "retrieve", "recall", "plan", "answer", "execute", "insufficient", "review", "commit_memory", "finalize"):
            builder.add_node(name, nodes.wrapped(name), retry_policy=RetryPolicy(max_attempts=1))
        builder.add_edge(START, "prepare")
        builder.add_conditional_edges("prepare", lambda state: "commit_memory" if state["task_type"] == "explicit_memory" else "retrieve",
                                      {"commit_memory": "commit_memory", "retrieve": "retrieve"})
        builder.add_edge("retrieve", "recall")
        builder.add_edge("recall", "plan")
        builder.add_conditional_edges("plan", lambda state: {"repo_qa": "answer", "code_change": "execute"}.get(state["task_type"], "insufficient"),
                                      {"answer": "answer", "execute": "execute", "insufficient": "insufficient"})
        builder.add_edge("execute", "review")
        builder.add_edge("review", "commit_memory")
        for name in ("answer", "insufficient", "commit_memory"):
            builder.add_edge(name, "finalize")
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=checkpointer)

    def _invoke(self, operation, budget, cancellation, *, resume_paused=False, on_event=None):
        if self.gateway_factory is None or self.knowledge_factory is None:
            raise WorkflowError("configuration_error", "workflow execution requires configured model and knowledge factories")
        workflow_id = operation["id"]
        # Missing input is an action item, never a fresh request.
        request = self.payloads.get(self.scope, operation["request_ref"], "request")
        original_budget = WorkflowBudget(**request["budget"])
        if budget is not None and budget != original_budget:
            raise WorkflowError("input_mismatch", "workflow budget does not match its durable request")
        budget = original_budget
        ledger = DurableBudgetLedger(self.directory / "budgets.db", workflow_id, budget,
                                     cancellation_token=cancellation, require_existing=bool(operation["budget_initialized"]))
        self.operations.mark_budget(self.scope, workflow_id)
        ledger.recover_pending()  # The caller holds this workflow's OS claim.
        gateway = self.gateway_factory(ledger)
        if gateway.ledger is not ledger:
            raise WorkflowError("configuration_error", "workflow gateway must use its durable budget")
        with self.knowledge_factory(ledger) as knowledge, sqlite_checkpointer(self.directory / "checkpoints.db") as checkpointer:
            if knowledge.scope.identity() != self.scope.identity() or knowledge.scope.root != self.scope.root:
                raise WorkflowError("configuration_error", "workflow knowledge must use the same trusted project scope")
            if knowledge.embeddings and knowledge.embeddings.ledger is not ledger:
                raise WorkflowError("configuration_error", "workflow Embedding and Chat must share the durable budget")
            nodes = WorkflowNodes(self, gateway, knowledge, ledger, cancellation, resume_paused=resume_paused, on_event=on_event)
            graph = self._build(nodes, checkpointer)
            config = {"configurable": {"thread_id": workflow_id}, "callbacks": [], "recursion_limit": 30}
            with _local_tracing():
                saved = graph.get_state(config)
                state = saved.values or {}
                if state and (state.get("owner_id") != self.scope.owner_id or state.get("project_id") != self.scope.project_id
                              or state.get("request_ref") != operation["request_ref"]
                              or state.get("configuration_hash") != self.configuration_hash):
                    raise WorkflowError("needs_action", "checkpoint identity does not match the trusted operation")
                if state.get("workflow_status") == "paused" and not resume_paused:
                    return self._outcome(state, ledger.snapshot())
                if state.get("workflow_status") in {"failed", "cancelled", "budget_exceeded", "needs_action"}:
                    return self._outcome(state, ledger.snapshot())
                if state.get("workflow_status") == "paused" and resume_paused:
                    graph.update_state(config, {"workflow_status": "running"}, as_node="plan")
                    saved = graph.get_state(config)
                if state and not saved.next and state.get("workflow_status") in {"completed", "failed", "cancelled", "budget_exceeded", "needs_action"}:
                    return self._outcome(state, ledger.snapshot())
                initial = {"schema_version": 1, "workflow_id": workflow_id, "owner_id": self.scope.owner_id,
                           "project_id": self.scope.project_id, "thread_id": operation["thread_id"], "turn_id": operation["turn_id"],
                           "request_ref": operation["request_ref"], "configuration_hash": self.configuration_hash,
                           "attempt": 0, "workflow_status": "pending", "answer_status": "not_started", "code_run_status": "not_started",
                           "fixture": bool(getattr(gateway.backend, "is_fixture", False))}
                try:
                    result = graph.invoke(None if saved.values else initial, config, durability="sync")
                except (BudgetExceeded, WorkflowError, KnowledgeError, MemoryError, GatewayError, StructuredOutputError, RunStoreError, ClaimBusy) as exc:
                    category = getattr(exc, "category", "needs_action")
                    category = getattr(category, "value", category)
                    if not isinstance(category, str) or re.fullmatch(r"[a-z][a-z_]{0,63}", category) is None:
                        category = "internal_error"
                    status = "budget_exceeded" if isinstance(exc, BudgetExceeded) else "cancelled" if category == "cancelled" else "needs_action"
                    graph.update_state(config, {"workflow_status": status, "stage": "stopped", "budget": ledger.snapshot(),
                                                "error_category": "budget_exceeded" if isinstance(exc, BudgetExceeded) else category})
                    result = graph.get_state(config).values
                return self._outcome(result, ledger.snapshot())

    def _outcome(self, state, budget):
        result = self.payloads.get(self.scope, state["result_ref"], "result") if state.get("result_ref") else None
        return WorkflowOutcome(state["workflow_id"], state.get("workflow_status", "pending"), state.get("answer_status", "not_started"),
            state.get("code_run_status", "not_started"), state.get("child_run_id", ""), state.get("memory_status", "not_requested"),
            state.get("stage", "pending"), state.get("error_category", ""), state.get("fixture", False), budget, result)

    def status(self, workflow_id: str) -> WorkflowOutcome:
        if re.fullmatch(r"[a-f0-9]{64}", workflow_id) is None:
            raise WorkflowError("configuration_error", "workflow ID must be the generated SHA-256 identifier")
        operation = self.operations.get(self.scope, workflow_id)
        budget = {"requests": 0, "tokens": 0, "pending_requests": 0, "unknown_usage_requests": 0,
                  "cost_usd": 0, "cost_unavailable": False}
        if operation["budget_initialized"]:
            budget = DurableBudgetLedger(self.directory / "budgets.db", workflow_id, require_existing=True).snapshot()
        with sqlite_checkpointer(self.directory / "checkpoints.db") as checkpointer:
            saved = checkpointer.get_tuple({"configurable": {"thread_id": workflow_id}})
        state = saved.checkpoint["channel_values"] if saved else {"workflow_id": workflow_id, "stage": "created"}
        return self._outcome(state, budget)
