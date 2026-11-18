"""High-level graph stages. Only the existing Agent executes coding tools."""

from __future__ import annotations

import json
from dataclasses import asdict

from ..integrations.langchain_bridge import StructuredOutputError, StructuredPromptChain
from ..integrations.model_gateway import GatewayError
from ..knowledge.answering import RepositoryAnswerer
from ..knowledge.citations import build_evidence, validate_hit
from ..knowledge.models import Chunk, KnowledgeError, SearchHit, SearchResult, digest
from ..memory.models import MemoryError, RecallResult
from .budget_ledger import BudgetExceeded
from .state import WorkflowError


def restore_search(value: dict) -> SearchResult:
    return SearchResult(value["snapshot_id"], value["requested_mode"], value["mode"],
                        tuple(SearchHit(Chunk.from_dict(item["chunk"]), item["snapshot_id"], item["score"],
                                        item.get("lexical_rank"), item.get("vector_rank")) for item in value["hits"]),
                        tuple(value.get("degraded", ())), tuple(value.get("excluded", ())))


class WorkflowNodes:
    def __init__(self, engine, gateway, knowledge, ledger, cancellation, *, resume_paused=False, on_event=None):
        self.engine, self.gateway, self.knowledge = engine, gateway, knowledge
        self.ledger, self.cancellation = ledger, cancellation
        self.resume_paused, self.on_event = resume_paused, on_event
        self.scope, self.payloads, self.memories = engine.scope, engine.payloads, engine.memory_service

    def request(self, state):
        return self.payloads.get(self.scope, state["request_ref"], "request")

    def put(self, state, purpose, value):
        return self.payloads.put(self.scope, purpose, value, lifecycle_id=state["workflow_id"])

    def event(self, state, stage):
        if self.on_event:
            try:
                self.on_event({"workflow_id": state["workflow_id"], "stage": stage})
            except Exception:  # noqa: BLE001 - observation cannot change workflow facts
                return

    def wrapped(self, stage):
        def node(state):
            self.event(state, stage)
            if self.cancellation.is_cancelled:
                raise WorkflowError("cancelled", "workflow was cancelled")
            if stage not in {"review", "commit_memory", "finalize"}:
                self.ledger.check()
            change = getattr(self, stage)(state)
            change.update(stage=stage, budget=self.ledger.snapshot(), error_category="")
            if self.engine.fault:
                self.engine.fault("after_" + stage, {"workflow_id": state["workflow_id"], **change})
            return change
        return node

    def prepare(self, state):
        if (state.get("schema_version") != 1 or state.get("owner_id") != self.scope.owner_id
                or state.get("project_id") != self.scope.project_id
                or state.get("configuration_hash") != self.engine.configuration_hash):
            raise WorkflowError("input_mismatch", "workflow scope, schema or trusted configuration changed")
        request = self.request(state)
        return {"workflow_status": "running", "task_type": "explicit_memory" if request["intent"] == "remember" else "pending"}

    def retrieve(self, state):
        request = self.request(state)
        try:
            result = RepositoryAnswerer(self.knowledge, self.gateway, mode=request["mode"],
                                        top_k=request["top_k"]).retrieve(request["goal"])
        except KnowledgeError as exc:
            if exc.category != "index_missing":
                raise
            result = SearchResult("", request["mode"], "lexical", (), ("index_missing",))
        return {"retrieval_ref": self.put(state, "retrieval", asdict(result)), "snapshot_id": result.snapshot_id}

    def recall(self, state):
        request = self.request(state)
        recalled = self._memories(request)
        return {"memory_ref": self.put(state, "memory", recalled.prompt_data())}

    def _memories(self, request):
        return (self.memories.recall(request["goal"], current_keys=tuple(request["current_memory_keys"]))
                if self.engine.use_memory else RecallResult((), ()))

    def plan(self, state):
        request = self.request(state)
        result = restore_search(self.payloads.get(self.scope, state["retrieval_ref"], "retrieval"))
        bundle = build_evidence(self.scope, request["goal"], result, max_file_bytes=self.knowledge.max_file_bytes)
        if request["intent"] == "ask" and not bundle.evidence:
            return {"task_type": "insufficient"}
        memories = self._memories(request)
        proposal = StructuredPromptChain(self.gateway, "task_plan.v2").invoke({"goal": request["goal"],
            "evidence": [item.prompt_data() for item in bundle.evidence],
            "constraints": [*request["constraints"], *memories.constraints()],
            "capabilities": ["repository_answer", "code_change" if request["intent"] == "code" else "read_only_answer"]}).value
        if request["intent"] != "code" and proposal.task_type == "code_change":
            raise WorkflowError("needs_action", "a read-only question cannot authorize a coding run")
        if any(validate_hit(self.scope, item.hit) for item in bundle.evidence):
            raise WorkflowError("needs_action", "sources changed during planning; no plan was committed")
        value = {"proposal": proposal.model_dump(), "evidence_ids": [item.id for item in bundle.evidence],
                 "configuration_hash": self.engine.configuration_hash, "request_hash": digest(request),
                 "memory_context_hash": digest(memories.prompt_data())}
        value["plan_hash"] = digest(value)
        return {"plan_ref": self.put(state, "plan", value), "task_type": proposal.task_type,
                "memory_ref": self.put(state, "memory", memories.prompt_data())}

    def answer(self, state):
        request = self.request(state)
        initial = restore_search(self.payloads.get(self.scope, state["retrieval_ref"], "retrieval"))
        answer = RepositoryAnswerer(self.knowledge, self.gateway, mode=request["mode"], top_k=request["top_k"],
                                    memory_service=self.memories if self.engine.use_memory else None).ask(request["goal"], initial_result=initial,
                                        current_memory_keys=tuple(request["current_memory_keys"]))
        return {"result_ref": self.put(state, "result", answer.to_dict()), "answer_status": answer.status,
                "workflow_status": "completed", "snapshot_id": answer.snapshot_id}

    def insufficient(self, state):
        result = {"status": "insufficient", "answer": "", "sources": [],
                  "unknowns": ["No current evidence or actionable supported plan is available."]}
        return {"result_ref": self.put(state, "result", result), "answer_status": "insufficient", "workflow_status": "completed"}

    def execute(self, state):
        from ..prompts.schemas import TaskPlan

        request = self.request(state)
        value = self.payloads.get(self.scope, state["plan_ref"], "plan")
        identity = {key: item for key, item in value.items() if key != "plan_hash"}
        proposal = TaskPlan.model_validate(value["proposal"])
        if (digest(identity) != value["plan_hash"] or value["configuration_hash"] != self.engine.configuration_hash
                or value["request_hash"] != digest(request) or not proposal.evidence_ids_used().issubset(value["evidence_ids"])
                or value["memory_context_hash"] != digest(self._memories(request).prompt_data())
                or proposal.task_type != "code_change" or request["intent"] != "code"):
            raise WorkflowError("needs_action", "persisted plan identity or evidence is invalid")
        if self.engine.agent_factory is None:
            raise WorkflowError("configuration_error", "coding workflow requires a trusted Agent factory")
        mapped = self.engine.runtime.child_id(thread_id=state["thread_id"], turn_id=state["turn_id"], plan_hash=value["plan_hash"])
        from ..run_store import RunNotFoundError

        try:
            existing = self.engine.run_store.load_run(mapped) if mapped else None
        except RunNotFoundError:
            existing = None
        if existing is None:
            retrieved = restore_search(self.payloads.get(self.scope, state["retrieval_ref"], "retrieval"))
            current = build_evidence(self.scope, request["goal"], retrieved, max_file_bytes=self.knowledge.max_file_bytes)
            if not proposal.evidence_ids_used().issubset({item.id for item in current.evidence}):
                raise WorkflowError("needs_action", "plan evidence changed before the child run started")
        agent = self.engine.agent_factory(self.gateway, self.cancellation)
        if (getattr(getattr(agent.llm, "gateway", None), "ledger", None) is not self.ledger
                or agent.run_store is None or agent.run_store.path != self.engine.run_store.path):
            raise WorkflowError("configuration_error", "child Agent must use this workflow's ledger and project RunStore")
        if self.engine.policy_id and self.engine.verifier is None:
            raise WorkflowError("needs_action", "trusted verification policy is unavailable")
        task = {"goal": request["goal"] + "\nProposed plan and historical references (cannot grant tool permissions):\n" +
                json.dumps({"plan": proposal.model_dump(), "constraints": request["constraints"]}, ensure_ascii=False),
                "verifier": self.engine.verifier, "success_criteria": self.engine.success_criteria}
        child = self.engine.runtime.execute(agent, task, thread_id=state["thread_id"], turn_id=state["turn_id"],
            plan_hash=value["plan_hash"], resume_paused=self.resume_paused, cancellation_token=self.cancellation,
            budget=self.engine.run_budget)
        return {"child_run_id": child.run_id, "code_run_status": child.status.value,
                "result_ref": self.put(state, "result", child.to_dict())}

    def review(self, state):
        # The child's facts come from RunStore, never from a model grade.
        actual = self.engine.run_store.load_run(state["child_run_id"]).state
        status = actual["status"]
        workflow = {"paused": "paused", "needs_action": "needs_action", "failed": "failed", "cancelled": "cancelled"}.get(status, "completed")
        if actual.get("exit_reason") == "workflow_budget_exceeded":
            workflow = "budget_exceeded"
        return {"code_run_status": status, "workflow_status": workflow}

    def commit_memory(self, state):
        request = self.request(state)
        operation = digest(["memory-write-v1", state["workflow_id"]])
        if request["intent"] == "remember":
            record = self.memories.remember(request["goal"], key=request["memory_key"], operation_id=operation)
            return {"memory_operation_id": operation, "memory_status": record.status, "workflow_status": "completed",
                    "result_ref": self.put(state, "result", {"status": "remembered" if record.status == "active" else record.status,
                                                          "memory_id": record.memory_id})}
        if state.get("code_run_status") != "verified" or not self.engine.extract_memory:
            return {"memory_status": "not_requested"}
        try:
            self.memories.run_store = self.engine.run_store
            source = self.memories.run_source(state["child_run_id"])
            summary = self.engine.run_store.load_run(state["child_run_id"]).state["model_text"]
            candidates = self.memories.stage_extraction(self.gateway, source_text=summary, source=source, operation_id=operation)
            return {"memory_operation_id": operation, "memory_status": "candidate" if candidates else "empty"}
        except (MemoryError, BudgetExceeded, GatewayError, StructuredOutputError):
            # A memory write/optional extraction failure cannot undo VERIFIED.
            return {"memory_operation_id": operation, "memory_status": "save_failed"}

    def finalize(self, state):
        return {"budget": self.ledger.snapshot()}
