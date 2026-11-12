"""Explicit, controller-owned state for one Agent run."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from .verification import VerificationResult, VerificationStatus


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING = "executing"
    REPLANNING = "replanning"
    PAUSED = "paused"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    NEEDS_ACTION = "needs_action"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExitReason(str, Enum):
    SUCCESS_CRITERIA_MET = "success_criteria_met"
    MODEL_STOPPED_UNVERIFIED = "model_stopped_unverified"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_ERROR = "verification_error"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    RECOVERY_REQUIRES_ACTION = "recovery_requires_action"
    MAX_ROUNDS = "max_rounds"
    MAX_TOOL_CALLS = "max_tool_calls"
    MAX_PROMPT_TOKENS = "max_prompt_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"
    MAX_COST_USD = "max_cost_usd"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    NO_PROGRESS = "no_progress"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    INTERNAL_ERROR = "internal_error"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({
    RunStatus.VERIFIED,
    RunStatus.UNVERIFIED,
    RunStatus.NEEDS_ACTION,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
})

# All status changes go through this table. In particular, a model response
# cannot jump RUNNING directly to VERIFIED: the controller must enter VERIFYING
# and make an independent decision.
ALLOWED_TRANSITIONS = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({
        RunStatus.WAITING_APPROVAL,
        RunStatus.EXECUTING,
        RunStatus.REPLANNING,
        RunStatus.PAUSED,
        RunStatus.VERIFYING,
        RunStatus.NEEDS_ACTION,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.WAITING_APPROVAL: frozenset({
        RunStatus.RUNNING,
        RunStatus.EXECUTING,
        RunStatus.REPLANNING,
        RunStatus.PAUSED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.EXECUTING: frozenset({
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.REPLANNING: frozenset({
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.PAUSED: frozenset({
        RunStatus.RUNNING,
        RunStatus.CANCELLED,
    }),
    RunStatus.VERIFYING: frozenset({
        RunStatus.VERIFIED,
        RunStatus.UNVERIFIED,
        RunStatus.NEEDS_ACTION,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.VERIFIED: frozenset(),
    RunStatus.UNVERIFIED: frozenset(),
    RunStatus.NEEDS_ACTION: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class RunStateError(RuntimeError):
    """Base class for state-machine domain errors."""


class InvalidStateTransition(RunStateError):
    def __init__(self, from_status: RunStatus, to_status: RunStatus):
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"illegal run-state transition: {from_status.value} -> {to_status.value}")


class UnknownPendingCall(RunStateError):
    def __init__(self, tool_call_id: str):
        self.tool_call_id = tool_call_id
        super().__init__(f"tool call is not pending: {tool_call_id}")


class VerificationEvidenceRequired(RunStateError):
    """Raised when code tries to mark a run verified without verifier evidence."""

    def __init__(self):
        super().__init__("verified state requires passed external verification evidence")


@dataclass(slots=True)
class RunState:
    run_id: str
    goal: str
    success_criteria: list[str] = field(default_factory=list)
    status: RunStatus = RunStatus.PENDING
    current_step: str = "created"
    round: int = 0
    pending_calls: list[dict[str, Any]] = field(default_factory=list)
    completed_calls: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    task_progress: list[str] = field(default_factory=list)
    budget_usage: dict[str, Any] = field(default_factory=dict)
    state_version: int = 1
    started_at: str = field(default_factory=_utc_now)
    finished_at: str | None = None
    exit_reason: ExitReason | None = None
    final_text: str = ""
    verification_status: VerificationStatus = VerificationStatus.NOT_RUN
    verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    failed_criteria: list[str] = field(default_factory=list)
    steering_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        goal: str,
        success_criteria: list[str] | None = None,
        run_id: str | None = None,
    ) -> "RunState":
        goal = goal.strip()
        if not goal:
            raise ValueError("task goal must not be empty")
        return cls(
            run_id=run_id or uuid4().hex,
            goal=goal,
            success_criteria=list(success_criteria or []),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunState":
        """Rehydrate a controller-owned snapshot from a validated RunStore row."""
        exit_reason = payload.get("exit_reason")
        return cls(
            run_id=str(payload["run_id"]),
            goal=str(payload["goal"]),
            success_criteria=list(payload.get("success_criteria") or []),
            status=RunStatus(payload.get("status", RunStatus.PENDING.value)),
            current_step=str(payload.get("current_step", "restored")),
            round=int(payload.get("round", 0)),
            pending_calls=[dict(item) for item in payload.get("pending_calls") or []],
            completed_calls=[dict(item) for item in payload.get("completed_calls") or []],
            evidence_refs=list(payload.get("evidence_refs") or []),
            task_progress=list(payload.get("task_progress") or []),
            budget_usage=dict(payload.get("budget_usage") or {}),
            state_version=int(payload.get("state_version", 1)),
            started_at=str(payload.get("started_at") or _utc_now()),
            finished_at=payload.get("finished_at"),
            exit_reason=ExitReason(exit_reason) if exit_reason else None,
            final_text=str(payload.get("model_text", payload.get("final_text", ""))),
            verification_status=VerificationStatus(
                payload.get("verification_status", VerificationStatus.NOT_RUN.value)
            ),
            verification_evidence=[dict(item) for item in payload.get("evidence") or []],
            failed_criteria=list(payload.get("failed_criteria") or []),
            steering_history=[
                dict(item) for item in payload.get("steering_history") or []
            ],
        )

    def prepare_resume(self, *, trace_run=None, resume_paused: bool = False) -> None:
        """Enter the controller's recovery path without pretending a normal transition.

        A persisted ``PAUSED`` run is a *safe stopped* state: recovery must not
        silently start new side effects.  Callers that are acting on an explicit
        user resume request pass ``resume_paused=True``; the default preserves
        ``PAUSED`` so automatic recovery cannot turn a pause into execution.
        """
        if self.status in TERMINAL_STATUSES:
            raise RunStateError(f"terminal run {self.run_id} cannot enter recovery")
        from_status = self.status
        before_version = self.state_version
        if from_status is RunStatus.PAUSED and not resume_paused:
            self.current_step = "paused_recovered"
            self.state_version += 1
            self._trace_transition(
                trace_run,
                from_status,
                RunStatus.PAUSED,
                before_version,
                self.state_version,
                outcome="paused",
            )
            return
        self.status = RunStatus.RUNNING
        self.current_step = "recovering"
        self.state_version += 1
        self._trace_transition(
            trace_run,
            from_status,
            RunStatus.RUNNING,
            before_version,
            self.state_version,
            outcome="recovered",
        )

    def transition(
        self,
        to_status: RunStatus,
        *,
        trace_run=None,
        current_step: str | None = None,
        exit_reason: ExitReason | None = None,
        final_text: str | None = None,
    ) -> None:
        from_status = self.status
        before_version = self.state_version
        if to_status not in ALLOWED_TRANSITIONS[from_status]:
            self._trace_transition(
                trace_run,
                from_status,
                to_status,
                before_version,
                before_version,
                outcome="rejected",
                error_category="invalid_state_transition",
            )
            raise InvalidStateTransition(from_status, to_status)

        if to_status is RunStatus.VERIFIED and (
            self.verification_status is not VerificationStatus.PASSED
            or not self.verification_evidence
        ):
            self._trace_transition(
                trace_run,
                from_status,
                to_status,
                before_version,
                before_version,
                outcome="rejected",
                error_category="verification_evidence_required",
            )
            raise VerificationEvidenceRequired

        if to_status in TERMINAL_STATUSES and exit_reason is None:
            raise ValueError(f"terminal status {to_status.value} requires an exit_reason")
        if to_status not in TERMINAL_STATUSES and exit_reason is not None:
            raise ValueError("exit_reason is only valid for a terminal status")

        self.status = to_status
        self.current_step = current_step or to_status.value
        self.state_version += 1
        if final_text is not None:
            self.final_text = final_text
        if to_status in TERMINAL_STATUSES:
            self.exit_reason = exit_reason
            self.finished_at = _utc_now()

        self._trace_transition(
            trace_run,
            from_status,
            to_status,
            before_version,
            self.state_version,
            outcome="success",
        )

    def advance_round(self, round_number: int) -> None:
        if self.status is not RunStatus.RUNNING:
            raise RunStateError(f"cannot advance round while status is {self.status.value}")
        if round_number <= self.round:
            raise RunStateError(f"round must increase: current={self.round}, requested={round_number}")
        self.round = round_number
        self.current_step = "calling_model"
        self.state_version += 1

    def plan_calls(self, calls: list[dict[str, Any]]) -> None:
        if self.status is not RunStatus.RUNNING:
            raise RunStateError(f"cannot plan tools while status is {self.status.value}")
        self.pending_calls.extend(dict(call) for call in calls)
        self.current_step = "planning_tools"
        self.state_version += 1

    def complete_call(
        self,
        tool_call_id: str,
        *,
        outcome: str,
        error_category: str | None = None,
        evidence_ref: str | None = None,
    ) -> None:
        index = next(
            (
                index
                for index, call in enumerate(self.pending_calls)
                if call.get("tool_call_id") == tool_call_id
            ),
            None,
        )
        if index is None:
            raise UnknownPendingCall(tool_call_id)
        call = self.pending_calls.pop(index)
        call.update({"outcome": outcome, "error_category": error_category})
        if evidence_ref is not None:
            call["evidence_ref"] = evidence_ref
            if evidence_ref not in self.evidence_refs:
                self.evidence_refs.append(evidence_ref)
        self.completed_calls.append(call)
        self.current_step = "collecting_tool_results"
        self.state_version += 1

    def set_step(self, step: str) -> None:
        self.current_step = step
        self.state_version += 1

    def update_task_progress(self, progress: list[str]) -> None:
        """Record controller-derived progress markers, never model-owned state."""
        progress = list(progress)
        if progress != self.task_progress:
            self.task_progress = progress
            self.state_version += 1

    def record_steering(self, metadata: dict[str, Any]) -> None:
        """Record controller metadata without storing the instruction text."""
        if self.status in TERMINAL_STATUSES:
            raise RunStateError("cannot apply steering to a terminal run")
        self.steering_history.append(dict(metadata))
        self.state_version += 1

    def record_verification(self, result: VerificationResult) -> None:
        """Persist a verifier-owned result while the state is VERIFYING."""
        if self.status is not RunStatus.VERIFYING:
            raise RunStateError(
                f"cannot record verification while status is {self.status.value}"
            )
        self.verification_status = result.status
        self.verification_evidence = [item.to_dict() for item in result.evidence]
        self.failed_criteria = list(result.failed_criteria)
        self.current_step = "verification_complete"
        self.state_version += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "status": self.status.value,
            "current_step": self.current_step,
            "round": self.round,
            "pending_calls": [dict(call) for call in self.pending_calls],
            "completed_calls": [dict(call) for call in self.completed_calls],
            "evidence_refs": list(self.evidence_refs),
            "task_progress": list(self.task_progress),
            "budget_usage": dict(self.budget_usage),
            "state_version": self.state_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "final_text": self.final_text,
            "model_text": self.final_text,
            "verification_status": self.verification_status.value,
            "evidence": [dict(item) for item in self.verification_evidence],
            "failed_criteria": list(self.failed_criteria),
            "steering_history": [dict(item) for item in self.steering_history],
        }

    def _trace_transition(
        self,
        trace_run,
        from_status: RunStatus,
        to_status: RunStatus,
        before_version: int,
        after_version: int,
        *,
        outcome: str,
        error_category: str | None = None,
    ) -> None:
        if trace_run is None:
            return
        trace_run.emit(
            "state_transition",
            round=self.round,
            state_version=after_version,
            outcome=outcome,
            error_category=error_category,
            details={
                "from_status": from_status.value,
                "to_status": to_status.value,
                "before_state_version": before_version,
                "after_state_version": after_version,
            },
        )


@dataclass(frozen=True, slots=True)
class AgentResult:
    state: RunState
    error_type: str | None = None
    _exception: BaseException | None = field(default=None, repr=False, compare=False)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return self.state.run_id

    @property
    def status(self) -> RunStatus:
        return self.state.status

    @property
    def exit_reason(self) -> ExitReason:
        if self.state.exit_reason is None:
            raise RunStateError("AgentResult was created before the run reached a terminal state")
        return self.state.exit_reason

    @property
    def final_text(self) -> str:
        return self.state.final_text

    @property
    def model_text(self) -> str:
        return self.state.final_text

    @property
    def verification_status(self) -> VerificationStatus:
        return self.state.verification_status

    @property
    def evidence(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self.state.verification_evidence)

    @property
    def failed_criteria(self) -> tuple[str, ...]:
        return tuple(self.state.failed_criteria)

    @property
    def display_text(self) -> str:
        """Compatibility string with an explicit marker unless independently verified."""
        if self.status is RunStatus.VERIFIED:
            return self.model_text
        if self.status is RunStatus.NEEDS_ACTION:
            marker = "[未验证：需要处理]"
        elif self.verification_status is VerificationStatus.ERROR:
            marker = "[未验证：验证错误]"
        else:
            marker = "[未验证]"
        return f"{marker} {self.model_text}".rstrip()

    @property
    def budget_usage(self) -> dict[str, Any]:
        return dict(self.state.budget_usage)

    def to_dict(self) -> dict[str, Any]:
        payload = self.state.to_dict()
        payload["error_type"] = self.error_type
        payload["metrics"] = dict(self.metrics)
        return payload

    def raise_for_error(self) -> None:
        if self._exception is not None:
            raise self._exception
