"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import hashlib
import inspect
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .budget import (
    CancellationToken,
    NoProgressObservation,
    RunBudget,
    RunBudgetController,
    RunStopped,
)
from .context import ContextManager, estimate_tokens
from .events import (
    AgentEventType,
    EventEmitter,
    build_event_sink,
    safe_tool_summary,
)
from .llm import LLM, ToolCall
from .metrics import RunMetrics
from .prompt import PLAN_MODE_PROMPT, system_prompt
from .run_store import (
    InjectedProcessCrash,
    RunStore,
    RunStoreCorruptError,
    RunStoreError,
    ToolCallStatus,
)
from .scheduler import (
    SchedulePlan,
    SchedulerCancelled,
    SchedulerInterrupted,
    ToolScheduler,
)
from .shell import run_shell
from .state import AgentResult, ExitReason, RunState, RunStatus
from .tool_cache import ToolCache
from .tools import ALL_TOOLS
from .tools.agent import AgentTool
from .tools.base import Tool
from .tools.bash import BashTool, get_tracked_cwd
from .tools.protocol import LegacyToolAdapter, ToolExecutionContext, ToolOutcome, ToolResult
from .tools.todo import TodoWriteTool
from .trace import TraceRecorder
from .verification import (
    CallableVerifierAdapter,
    VerificationCommandResult,
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
)

if TYPE_CHECKING:
    from .runtime import AgentRunHandle, ControlInbox

MAX_ROUNDS_TEXT = "(reached maximum tool-call rounds)"
STEERING_MESSAGE_PREFIX = "[Steering instruction received during the run]"


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        permission=None,
        hooks=None,
        trace_sink=None,
        run_store: RunStore | None = None,
        crash_injector=None,
        tool_cache: ToolCache | bool | None = None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else ALL_TOOLS
        self.permission = permission
        self.hooks = hooks
        self.trace = TraceRecorder(trace_sink)
        self.trace_sink = self.trace.sink
        self.run_store = run_store
        self.crash_injector = crash_injector
        if tool_cache is False:
            self.tool_cache = ToolCache()
            self.tool_cache.enabled = False
        elif isinstance(tool_cache, ToolCache):
            self.tool_cache = tool_cache
        elif tool_cache is None or tool_cache is True:
            self.tool_cache = ToolCache()
        else:
            raise TypeError("tool_cache must be a ToolCache, False, or None")
        self.last_metrics: dict = {}
        self.last_run_id: str | None = None
        self._pending_exploration_reminder: dict | None = None
        self._tool_by_name = {t.name: t for t in self.tools}
        self._tool_adapters = {
            name: LegacyToolAdapter(tool) for name, tool in self._tool_by_name.items()
        }
        self.tool_scheduler = ToolScheduler(self._tool_by_name)
        self.tool_results: list[ToolResult] = []
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self._system = system_prompt(self.tools)
        self.plan_mode = False  # toggled by /plan; while on, mutating tools are refused
        self._active_run_lock = threading.Lock()
        self._active_run_claim = None

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

        self._todo = next((t for t in self.tools if isinstance(t, TodoWriteTool)), None)

    def _full_messages(self) -> list[dict]:
        system = self._system
        # re-injected every round, like the task list below, so a toggle made
        # between turns takes effect on the very next request
        if self.plan_mode:
            system += "\n\n" + PLAN_MODE_PROMPT
        # the task list is re-injected every round, so the model always sees the
        # current state rather than a stale copy buried in old tool results
        if self._todo is not None:
            rendered = self._todo.render()
            if rendered:
                system += "\n\n# Current task list\n" + rendered
        return [{"role": "system", "content": system}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def _claim_active_run(self):
        """Atomically reserve this mutable Agent for one run."""
        from .runtime import AgentBusyError

        with self._active_run_lock:
            if self._active_run_claim is not None:
                raise AgentBusyError(
                    "This Agent already has an active run; wait for it to finish "
                    "before starting another."
                )
            claim = object()
            self._active_run_claim = claim
            return claim

    def _validate_active_run_claim(self, claim) -> None:
        from .runtime import AgentBusyError

        with self._active_run_lock:
            if claim is None or claim is not self._active_run_claim:
                raise AgentBusyError("The active Agent run claim is invalid or expired.")

    def _release_active_run(self, claim) -> None:
        with self._active_run_lock:
            if claim is self._active_run_claim:
                self._active_run_claim = None

    def start(
        self,
        task_spec,
        on_token=None,
        on_tool=None,
        on_reasoning=None,
        *,
        on_event=None,
        event_sink=None,
        budget: RunBudget | None = None,
        cancellation_token: CancellationToken | None = None,
        event_queue_size: int = 256,
        token_chunk_chars: int = 4096,
    ) -> "AgentRunHandle":
        """Start the synchronous run loop in one observable worker thread."""
        from .runtime import start_agent_run

        return start_agent_run(
            self,
            task_spec,
            on_token=on_token,
            on_tool=on_tool,
            on_reasoning=on_reasoning,
            on_event=on_event,
            event_sink=event_sink,
            budget=budget,
            cancellation_token=cancellation_token,
            event_queue_size=event_queue_size,
            token_chunk_chars=token_chunk_chars,
        )

    def chat(
        self,
        user_input: str,
        on_token=None,
        on_tool=None,
        on_reasoning=None,
        *,
        on_event=None,
        event_sink=None,
        budget: RunBudget | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> str:
        """Backward-compatible string API over the structured run state machine."""
        result = self.run(
            user_input,
            on_token=on_token,
            on_tool=on_tool,
            on_reasoning=on_reasoning,
            on_event=on_event,
            event_sink=event_sink,
            budget=budget,
            cancellation_token=cancellation_token,
        )
        result.raise_for_error()
        return result.display_text

    def run(
        self,
        task_spec,
        on_token=None,
        on_tool=None,
        on_reasoning=None,
        *,
        on_event=None,
        event_sink=None,
        budget: RunBudget | None = None,
        cancellation_token: CancellationToken | None = None,
        _resume_run_id: str | None = None,
        _run_id: str | None = None,
        _run_claim=None,
        _event_emitter: EventEmitter | None = None,
        _control_inbox: "ControlInbox | None" = None,
        _metrics: RunMetrics | None = None,
        _resume_paused: bool = False,
    ) -> AgentResult:
        """Run one task and return a serializable, controller-owned result.

        ``task_spec`` may be a goal string or a mapping with ``goal``, optional
        ``success_criteria`` strings, and an optional trusted ``Verifier``.
        Legacy snapshot callables remain adapted. Model output alone is never
        enough to produce VERIFIED.
        """
        claimed_here = _run_claim is None
        if claimed_here:
            _run_claim = self._claim_active_run()
        else:
            self._validate_active_run_claim(_run_claim)
        try:
            return self._run_impl(
                task_spec,
                on_token=on_token,
                on_tool=on_tool,
                on_reasoning=on_reasoning,
                on_event=on_event,
                event_sink=event_sink,
                budget=budget,
                cancellation_token=cancellation_token,
                _resume_run_id=_resume_run_id,
                _run_id=_run_id,
                _event_emitter=_event_emitter,
                _control_inbox=_control_inbox,
                _metrics=_metrics,
                _resume_paused=_resume_paused,
            )
        finally:
            if claimed_here:
                self._release_active_run(_run_claim)

    def _run_impl(
        self,
        task_spec,
        on_token=None,
        on_tool=None,
        on_reasoning=None,
        *,
        on_event=None,
        event_sink=None,
        budget: RunBudget | None = None,
        cancellation_token: CancellationToken | None = None,
        _resume_run_id: str | None = None,
        _run_id: str | None = None,
        _event_emitter: EventEmitter | None = None,
        _control_inbox: "ControlInbox | None" = None,
        _metrics: RunMetrics | None = None,
        _resume_paused: bool = False,
    ) -> AgentResult:
        """Implementation of :meth:`run`; callers must hold the active-run claim."""
        goal, success_criteria, verifier = self._normalise_task_spec(task_spec)
        explicit_budget = budget is not None
        budget = budget if budget is not None else RunBudget(max_rounds=self.max_rounds)
        cancellation_token = cancellation_token or CancellationToken()
        model = getattr(self.llm, "model", None)
        resumed = _resume_run_id is not None
        if resumed:
            if self.run_store is None:
                raise RunStoreError("resume_run requires an Agent configured with RunStore")
            stored = self.run_store.load_run(_resume_run_id)
            try:
                state = RunState.from_dict(stored.state)
            except (KeyError, TypeError, ValueError) as exc:
                raise RunStoreCorruptError(
                    "Persisted run state is invalid; refusing unsafe recovery."
                ) from exc
            goal = state.goal
            success_criteria = list(state.success_criteria)
            self.messages = [dict(message) for message in stored.messages]
            trace_run = self.trace.new_run(model, run_id=state.run_id)
        else:
            trace_run = self.trace.new_run(model, run_id=_run_id)
            state = RunState.create(goal, success_criteria, run_id=trace_run.run_id)
        self.last_run_id = state.run_id
        event_emitter = _event_emitter or EventEmitter(
            state.run_id,
            build_event_sink(on_event=on_event, event_sink=event_sink),
        )
        if event_emitter.run_id != state.run_id:
            raise ValueError("injected event emitter run_id does not match the Agent run")
        metrics = _metrics if _metrics is not None else RunMetrics()
        if _metrics is None:
            event_emitter.add_observer(metrics.observe)
        self.last_metrics = {}
        # A run may start after an external editor changed files; never reuse
        # read results from an earlier turn.  Within this run the cache must
        # still be invalidated by every mutating tool call.
        self.tool_cache.invalidate("run_start")
        self._pending_exploration_reminder = None
        control_inbox = _control_inbox
        if control_inbox is not None:
            control_inbox.update_position(state.state_version, state.round)
        budget_controller = RunBudgetController(budget, cancellation_token, self.llm)
        if resumed:
            usage = state.budget_usage
            budget_controller.usage.rounds = max(state.round, int(usage.get("rounds", 0)))
            budget_controller.usage.tool_calls = max(
                len(state.completed_calls), int(usage.get("tool_calls", 0))
            )
            budget_controller.usage.prompt_tokens = int(usage.get("prompt_tokens", 0))
            budget_controller.usage.completion_tokens = int(
                usage.get("completion_tokens", 0)
            )
            budget_controller.usage.cost_usd = usage.get("cost_usd")
            budget_controller.usage.cost_unavailable = bool(
                usage.get("cost_unavailable", False)
            )
        run_timer = trace_run.timer()
        trace_run.emit(
            "run_started",
            timer=run_timer,
            outcome="started",
            state_version=state.state_version,
        )
        event_emitter.emit(
            AgentEventType.RUN_STARTED,
            phase="starting",
            message="Run started",
            public_data={
                "resumed": resumed,
                "status": state.status.value,
            },
        )
        run_finished = False

        def finish_run(error_category: str | None = None):
            nonlocal run_finished
            if run_finished:
                return
            run_finished = True
            trace_run.emit(
                "run_finished",
                timer=run_timer,
                finished=True,
                prompt_tokens=budget_controller.usage.prompt_tokens,
                completion_tokens=budget_controller.usage.completion_tokens,
                estimated_cost=budget_controller.usage.cost_usd,
                outcome=state.exit_reason.value if state.exit_reason else "unfinished",
                error_category=error_category,
                round=state.round,
                state_version=state.state_version,
                details={
                    "status": state.status.value,
                    "budget": budget.to_dict(),
                    "budget_usage": budget_controller.usage.to_dict(),
                    "cost_unavailable": budget_controller.usage.cost_unavailable,
                    "verification_status": state.verification_status.value,
                    "verification_evidence_count": len(state.verification_evidence),
                },
            )
            if self.run_store is not None:
                self.run_store.finalize_steering(
                    state.run_id,
                    state.status.value,
                )
            self._persist_boundary(state, trace_run, "run_finished")
            terminal_type = AgentEventType.RUN_FINISHED
            terminal_phase = "finished"
            terminal_message = "Run finished"
            if state.status is RunStatus.CANCELLED:
                terminal_type = AgentEventType.RUN_CANCELLED
                terminal_phase = "cancelled"
                terminal_message = "Run cancelled"
            elif state.status is RunStatus.FAILED:
                terminal_type = AgentEventType.RUN_FAILED
                terminal_phase = "failed"
                terminal_message = "Run failed"
            event_emitter.emit(
                terminal_type,
                phase=terminal_phase,
                message=terminal_message,
                public_data={
                    "status": state.status.value,
                    "exit_reason": (
                        state.exit_reason.value if state.exit_reason else None
                    ),
                    "error_category": error_category,
                    "round": state.round,
                    "verification_status": state.verification_status.value,
                    "budget_usage": budget_controller.usage.to_dict(),
                },
            )
            self.last_metrics = metrics.to_dict()

        active_tool_calls = None
        try:
            if resumed and state.status in {
                RunStatus.VERIFIED,
                RunStatus.UNVERIFIED,
                RunStatus.NEEDS_ACTION,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                finish_run()
                return AgentResult(state=state, metrics=metrics.to_dict())

            if resumed and state.status is RunStatus.VERIFYING:
                self._persist_boundary(state, trace_run, "resume_before_verification")
                self._crash_at("verification_started", state)
                verification = self._verify(
                    verifier,
                    state,
                    trace_run,
                    model_text=state.final_text,
                    event_emitter=event_emitter,
                )
                self._crash_at("verification_after_execute_before_result", state)
                state.record_verification(verification)
                budget_controller.check("after_verification", state, trace_run)
                self._persist_boundary(state, trace_run, "after_verification")
                self._finish_verification(state, verification, trace_run, state.final_text)
                finish_run()
                return AgentResult(state=state, metrics=metrics.to_dict())

            if resumed:
                state.prepare_resume(
                    trace_run=trace_run,
                    resume_paused=_resume_paused,
                )
                if state.status is RunStatus.PAUSED:
                    # Automatic recovery of a paused run must not begin model
                    # calls or side effects.  The caller has to opt in
                    # explicitly with resume_paused=True.
                    self._persist_boundary(state, trace_run, "resume_paused")
                    self.last_metrics = metrics.to_dict()
                    return AgentResult(state=state, metrics=metrics.to_dict())

                pending_controls = self.run_store.pending_steering(state.run_id)
                if pending_controls:
                    instruction_ids = [
                        item.instruction_id for item in pending_controls
                    ]
                    self.run_store.mark_steering_needs_action(
                        state.run_id,
                        instruction_ids,
                        reason=(
                            "received but not applied before recovery; the "
                            "instruction body is intentionally not persisted"
                        ),
                    )
                    text = (
                        "Recovery stopped at an unapplied steering instruction. "
                        "Its body was not persisted, so it cannot be replayed "
                        "safely. Resubmit the instruction or confirm a manual "
                        "retry."
                    )
                    state.transition(
                        RunStatus.NEEDS_ACTION,
                        trace_run=trace_run,
                        current_step="steering_needs_action",
                        exit_reason=ExitReason.RECOVERY_REQUIRES_ACTION,
                        final_text=text,
                    )
                    self._persist_boundary(
                        state, trace_run, "steering_needs_action"
                    )
                    finish_run()
                    return AgentResult(state=state, metrics=metrics.to_dict())

                self._persist_boundary(state, trace_run, "resume_started")
                stopped = self._recover_pending_calls(
                    state,
                    trace_run,
                    verifier,
                    event_emitter=event_emitter,
                )
                if stopped is not None:
                    finish_run()
                    return AgentResult(
                        state=stopped.state,
                        error_type=stopped.error_type,
                        metrics=metrics.to_dict(),
                    )
            else:
                state.transition(
                    RunStatus.RUNNING,
                    trace_run=trace_run,
                    current_step="starting",
                )
                self.messages.append({"role": "user", "content": goal})
                self._persist_boundary(state, trace_run, "run_started")
                budget_controller.check("before_aux_model", state, trace_run)
                self._maybe_compress_traced(trace_run, round_no=0)
                budget_controller.check("after_model", state, trace_run)

            while True:
                self._control_checkpoint(
                    control_inbox,
                    budget_controller,
                    cancellation_token,
                    state,
                    trace_run,
                    event_emitter,
                    "before_model",
                )
                budget_controller.check("before_model", state, trace_run)
                if self._pending_exploration_reminder is not None:
                    reminder = self._pending_exploration_reminder
                    self._pending_exploration_reminder = None
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "[exploration_reminder] The same tool call produced an "
                            "unchanged result again. Do not repeat it. "
                            "1. Summarize the evidence you already collected, including "
                            "the relevant file paths and line ranges. "
                            "2. Re-plan the remaining work from that evidence. "
                            "3. Prefer glob for discovery, grep for search, and "
                            "read_file with a wider offset/limit instead of repeated "
                            "sed/cat/grep shell calls."
                        ),
                    })
                    trace_run.emit(
                        "exploration_reminder",
                        round=state.round,
                        state_version=state.state_version,
                        tool_name=reminder["tool_name"],
                        outcome="repeated_without_progress",
                        details={
                            "action_fingerprint": reminder["fingerprint"],
                            "no_progress_steps": reminder["count"],
                        },
                    )
                budget_controller.start_round(state)
                round_no = budget_controller.usage.rounds
                state.advance_round(round_no)
                if control_inbox is not None:
                    control_inbox.update_position(state.state_version, state.round)
                self._persist_boundary(state, trace_run, "before_model")
                model_timer = trace_run.timer()
                trace_run.emit(
                    "model_started",
                    timer=model_timer,
                    round=round_no,
                    state_version=state.state_version,
                    outcome="started",
                )
                event_emitter.emit(
                    AgentEventType.MODEL_STARTED,
                    phase="model",
                    message="Model started",
                    public_data={
                        "model": model,
                        "round": round_no,
                    },
                )
                reported_activity: set[tuple[str, int | None]] = set()

                def handle_stream_activity(
                    activity,
                    *,
                    _reported_activity=reported_activity,
                    _round_no=round_no,
                ) -> None:
                    item = activity if isinstance(activity, Mapping) else {}
                    kind = str(item.get("activity") or "stream_activity")
                    raw_index = item.get("tool_index")
                    tool_index = raw_index if isinstance(raw_index, int) else None
                    key = (kind, tool_index)
                    if key in _reported_activity:
                        return
                    _reported_activity.add(key)
                    message = (
                        "Model is preparing a tool call"
                        if kind == "tool_call_detected"
                        else "Model is analyzing"
                        if kind == "reasoning"
                        else "Model stream is active"
                    )
                    data = {"activity": kind, "round": _round_no}
                    if tool_index is not None:
                        data["tool_index"] = tool_index
                    event_emitter.emit(
                        AgentEventType.MODEL_STREAM_ACTIVITY,
                        phase="model",
                        message=message,
                        public_data=data,
                    )

                def handle_token(token, *, _round_no=round_no) -> None:
                    event_emitter.emit(
                        AgentEventType.ASSISTANT_TOKEN,
                        phase="model",
                        message=str(token),
                        public_data={"round": _round_no},
                    )
                    if on_token:
                        on_token(token)

                def handle_reasoning(
                    token,
                    *,
                    _handle_activity=handle_stream_activity,
                ) -> None:
                    _handle_activity({"activity": "reasoning"})
                    if on_reasoning:
                        on_reasoning(token)

                try:
                    model_kwargs = {
                        "messages": self._full_messages(),
                        "tools": self._tool_schemas(),
                        "on_token": handle_token,
                        "on_reasoning": handle_reasoning,
                    }
                    if self._accepts_keyword(
                        self.llm.chat,
                        "on_stream_activity",
                    ):
                        model_kwargs["on_stream_activity"] = handle_stream_activity
                    resp = self.llm.chat(
                        **model_kwargs,
                    )
                except BaseException as exc:
                    trace_run.emit(
                        "model_failed",
                        timer=model_timer,
                        finished=True,
                        round=round_no,
                        state_version=state.state_version,
                        outcome="failed",
                        error_category=type(exc).__name__,
                    )
                    event_emitter.emit(
                        AgentEventType.MODEL_FAILED,
                        phase="model",
                        message="Model failed",
                        public_data={
                            "round": round_no,
                            "error_category": type(exc).__name__,
                        },
                    )
                    raise
                budget_controller.record_model_response(resp, state)
                trace_run.emit(
                    "model_finished",
                    timer=model_timer,
                    finished=True,
                    prompt_tokens=getattr(resp, "prompt_tokens", None),
                    completion_tokens=getattr(resp, "completion_tokens", None),
                    estimated_cost=budget_controller.usage.cost_usd,
                    round=round_no,
                    state_version=state.state_version,
                    outcome="success",
                    details={
                        "budget_usage": budget_controller.usage.to_dict(),
                        "cost_unavailable": budget_controller.usage.cost_unavailable,
                    },
                )
                event_emitter.emit(
                    AgentEventType.MODEL_FINISHED,
                    phase="model",
                    message="Model finished",
                    public_data={
                        "round": round_no,
                        "prompt_tokens": getattr(resp, "prompt_tokens", None),
                        "completion_tokens": getattr(
                            resp,
                            "completion_tokens",
                            None,
                        ),
                        "tool_call_count": len(resp.tool_calls),
                        "has_text": bool(resp.content),
                    },
                )
                budget_controller.check("after_model", state, trace_run)

                # Text means the model stopped, not that the task is verified.
                if not resp.tool_calls:
                    self.messages.append(resp.message)
                    applied, _redirected = self._control_checkpoint(
                        control_inbox,
                        budget_controller,
                        cancellation_token,
                        state,
                        trace_run,
                        event_emitter,
                        "before_verification",
                        close_if_empty=True,
                    )
                    if applied:
                        continue
                    state.transition(
                        RunStatus.VERIFYING,
                        trace_run=trace_run,
                        current_step="verifying",
                        final_text=resp.content,
                    )
                    self._persist_boundary(
                        state,
                        trace_run,
                        "after_model_before_verification",
                    )
                    self._crash_at("verification_started", state)
                    verification = self._verify(
                        verifier,
                        state,
                        trace_run,
                        model_text=resp.content,
                        event_emitter=event_emitter,
                    )
                    self._crash_at("verification_after_execute_before_result", state)
                    state.record_verification(verification)
                    budget_controller.check("after_verification", state, trace_run)
                    self._persist_boundary(state, trace_run, "after_verification")
                    self._finish_verification(
                        state,
                        verification,
                        trace_run,
                        resp.content,
                    )
                    finish_run()
                    return AgentResult(state=state, metrics=metrics.to_dict())

                # tool calls -> execute (parallel when multiple, like Claude Code's
                # StreamingToolExecutor which runs independent tools concurrently)
                active_tool_calls = resp.tool_calls
                state.plan_calls([
                    {
                        "tool_call_id": tc.id,
                        "tool_name": tc.name,
                        "round": round_no,
                    }
                    for tc in resp.tool_calls
                ])
                self.messages.append(resp.message)
                if self.run_store is not None:
                    self.run_store.plan_tool_calls(
                        state,
                        self.messages,
                        resp.tool_calls,
                        self._tool_by_name,
                        model=model,
                        trace_id=trace_run.trace_id,
                        trace_ref=self._trace_ref(),
                    )
                self._crash_at("after_tool_planned", state)
                for tc in resp.tool_calls:
                    trace_run.emit(
                        "tool_planned",
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        round=round_no,
                        state_version=state.state_version,
                        outcome="planned",
                    )
                    event_emitter.emit(
                        AgentEventType.TOOL_PLANNED,
                        phase="planning_tools",
                        message=f"Tool planned: {tc.name}",
                        public_data={
                            "tool_call_id": tc.id,
                            **safe_tool_summary(tc.name, tc.arguments),
                            "round": round_no,
                        },
                    )
                _applied, redirected = self._control_checkpoint(
                    control_inbox,
                    budget_controller,
                    cancellation_token,
                    state,
                    trace_run,
                    event_emitter,
                    "after_model_before_tools",
                    redirect_only=True,
                    outstanding_tool_calls=resp.tool_calls,
                )
                if redirected:
                    active_tool_calls = None
                    continue
                state.transition(
                    RunStatus.WAITING_APPROVAL,
                    trace_run=trace_run,
                    current_step="checking_permissions",
                )
                schedule = self.tool_scheduler.plan(resp.tool_calls)
                self.tool_scheduler.record_decision(
                    schedule,
                    trace_run,
                    round_no=round_no,
                    state_version=state.state_version,
                )
                queue_timers = [trace_run.timer() for _ in resp.tool_calls]

                if len(resp.tool_calls) == 1:
                    tc = resp.tool_calls[0]
                    _applied, redirected = self._control_checkpoint(
                        control_inbox,
                        budget_controller,
                        cancellation_token,
                        state,
                        trace_run,
                        event_emitter,
                        "before_each_serial_tool",
                        redirect_only=True,
                        outstanding_tool_calls=(tc,),
                    )
                    if redirected:
                        active_tool_calls = None
                        continue
                    self.tool_scheduler.record_queue_wait(
                        tc,
                        trace_run,
                        queue_timers[0],
                        parallel=False,
                        round_no=round_no,
                        state_version=state.state_version,
                    )
                    budget_controller.before_tools([tc], state, trace_run)
                    result = self._exec_single_tool(
                        tc,
                        state,
                        trace_run,
                        round_no,
                        on_tool,
                        event_emitter,
                        cancellation_token,
                    )
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.model_content(),
                    })
                    self._sync_task_progress(state, tc, result)
                    self._queue_exploration_reminder(
                        budget_controller.observe_tool(tc, result, state, trace_run)
                    )
                    self._persist_boundary(state, trace_run, "after_tool_receipt")
                    self._control_checkpoint(
                        control_inbox,
                        budget_controller,
                        cancellation_token,
                        state,
                        trace_run,
                        event_emitter,
                        "after_tool",
                    )
                elif schedule.parallel:
                    budget_controller.before_tools(resp.tool_calls, state, trace_run)
                    results = self._exec_tool_results_parallel(
                        resp.tool_calls,
                        on_tool,
                        trace_run=trace_run,
                        round_no=round_no,
                        state=state,
                        schedule=schedule,
                        event_emitter=event_emitter,
                        cancellation_probe=lambda: cancellation_token.is_cancelled,
                        cancellation_token=cancellation_token,
                    )
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result.model_content(),
                        })
                        self._sync_task_progress(state, tc, result)
                        self._queue_exploration_reminder(
                            budget_controller.observe_tool(tc, result, state, trace_run)
                        )
                    self._persist_boundary(state, trace_run, "after_tool_receipts")
                    self._control_checkpoint(
                        control_inbox,
                        budget_controller,
                        cancellation_token,
                        state,
                        trace_run,
                        event_emitter,
                        "after_parallel_batch",
                    )
                else:
                    # Unsafe or stateful tools execute deterministically in
                    # model order; metadata, not tool names, drives this.
                    serial_redirected = False
                    for index, tc in enumerate(resp.tool_calls):
                        if index:
                            state.transition(
                                RunStatus.WAITING_APPROVAL,
                                trace_run=trace_run,
                                current_step="checking_permissions",
                            )
                        _applied, redirected = self._control_checkpoint(
                            control_inbox,
                            budget_controller,
                            cancellation_token,
                            state,
                            trace_run,
                            event_emitter,
                            "before_each_serial_tool",
                            redirect_only=True,
                            outstanding_tool_calls=resp.tool_calls[index:],
                        )
                        if redirected:
                            active_tool_calls = None
                            serial_redirected = True
                            break
                        self.tool_scheduler.record_queue_wait(
                            tc,
                            trace_run,
                            queue_timers[index],
                            parallel=False,
                            round_no=round_no,
                            state_version=state.state_version,
                        )
                        budget_controller.before_tools([tc], state, trace_run)
                        result = self._exec_single_tool(
                            tc,
                            state,
                            trace_run,
                            round_no,
                            on_tool,
                            event_emitter,
                            cancellation_token,
                        )
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result.model_content(),
                        })
                        self._sync_task_progress(state, tc, result)
                        self._queue_exploration_reminder(
                            budget_controller.observe_tool(tc, result, state, trace_run)
                        )
                        self._persist_boundary(state, trace_run, "after_tool_receipt")
                        _applied, redirected = self._control_checkpoint(
                            control_inbox,
                            budget_controller,
                            cancellation_token,
                            state,
                            trace_run,
                            event_emitter,
                            "after_tool",
                            redirect_only=index < len(resp.tool_calls) - 1,
                            outstanding_tool_calls=resp.tool_calls[index + 1:],
                        )
                        if redirected:
                            active_tool_calls = None
                            serial_redirected = True
                            break
                    if serial_redirected:
                        continue

                active_tool_calls = None

                # compress if tool outputs are big
                budget_controller.check("before_aux_model", state, trace_run)
                self._maybe_compress_traced(trace_run, round_no=round_no)
                budget_controller.check("after_model", state, trace_run)
                budget_controller.check("before_next_round", state, trace_run)

        except RunStopped as stop:
            self._finish_pending_tool_calls(
                active_tool_calls,
                state,
                trace_run,
                f"[stopped] {stop.message}",
                "cancelled" if stop.cancelled else "budget_exhausted",
                event_emitter=event_emitter,
            )
            final_text = (
                MAX_ROUNDS_TEXT
                if stop.exit_reason is ExitReason.MAX_ROUNDS and not explicit_budget
                else stop.message
            )
            state.transition(
                RunStatus.CANCELLED if stop.cancelled else RunStatus.FAILED,
                trace_run=trace_run,
                exit_reason=stop.exit_reason,
                final_text=final_text,
            )
            finish_run()
            return AgentResult(state=state, metrics=metrics.to_dict())
        except KeyboardInterrupt as exc:
            cancellation_token.cancel("Keyboard interrupt requested by the user.")
            self._finish_pending_tool_calls(
                active_tool_calls,
                state,
                trace_run,
                "[interrupted]",
                "interrupted",
                event_emitter=event_emitter,
            )
            if state.status not in {RunStatus.CANCELLED, RunStatus.VERIFIED,
                                    RunStatus.UNVERIFIED, RunStatus.NEEDS_ACTION,
                                    RunStatus.FAILED}:
                state.transition(
                    RunStatus.CANCELLED,
                    trace_run=trace_run,
                    exit_reason=ExitReason.INTERRUPTED,
                )
            finish_run("KeyboardInterrupt")
            return AgentResult(
                state=state,
                error_type=type(exc).__name__,
                metrics=metrics.to_dict(),
                _exception=exc,
            )
        except InjectedProcessCrash:
            # A crash injection represents abrupt process loss: do not backfill
            # messages or rewrite STARTED records, because recovery must observe
            # precisely what was durable at that boundary.
            raise
        except BaseException as exc:  # noqa: BLE001 - preserve error for chat()
            self._finish_pending_tool_calls(
                active_tool_calls,
                state,
                trace_run,
                "[stopped] Run stopped because of an internal error.",
                "internal_error",
                event_emitter=event_emitter,
            )
            reason = self._failure_exit_reason(state)
            if state.status not in {RunStatus.CANCELLED, RunStatus.VERIFIED,
                                    RunStatus.UNVERIFIED, RunStatus.NEEDS_ACTION,
                                    RunStatus.FAILED}:
                state.transition(
                    RunStatus.FAILED,
                    trace_run=trace_run,
                    exit_reason=reason,
                )
            finish_run(type(exc).__name__)
            return AgentResult(
                state=state,
                error_type=type(exc).__name__,
                metrics=metrics.to_dict(),
                _exception=exc,
            )

    def resume_run(
        self,
        run_id: str,
        *,
        verifier=None,
        on_token=None,
        on_tool=None,
        on_reasoning=None,
        on_event=None,
        event_sink=None,
        budget: RunBudget | None = None,
        cancellation_token: CancellationToken | None = None,
        resume_paused: bool = True,
    ) -> AgentResult:
        """Resume a persisted run without trusting model-owned recovery state.

        ``resume_paused`` is explicit user consent to leave a persisted
        ``PAUSED`` state.  It defaults to ``True`` because calling this API is
        itself an explicit resume request; automatic recovery never calls it.
        """
        task_spec = {"goal": "resume persisted run", "verifier": verifier}
        return self.run(
            task_spec,
            on_token=on_token,
            on_tool=on_tool,
            on_reasoning=on_reasoning,
            on_event=on_event,
            event_sink=event_sink,
            budget=budget,
            cancellation_token=cancellation_token,
            _resume_run_id=run_id,
            _resume_paused=resume_paused,
        )

    def _steering_checkpoint(
        self,
        control_inbox,
        state: RunState,
        trace_run,
        event_emitter: EventEmitter,
        safe_point: str,
        *,
        redirect_only: bool = False,
        close_if_empty: bool = False,
        outstanding_tool_calls=(),
    ) -> tuple[bool, bool]:
        """Apply queued controls at a provider-valid Agent boundary.

        Returns ``(applied_any, redirected)``.  Append-only instructions stay
        queued while an assistant tool-call message still needs replies.
        """
        if control_inbox is None:
            return False, False
        from .runtime import SteeringMode

        control_inbox.update_position(state.state_version, state.round)
        instructions = control_inbox.take(
            redirect_only=redirect_only,
            close_if_empty=close_if_empty,
        )
        if not instructions:
            return False, False

        redirected = any(
            instruction.mode is SteeringMode.REDIRECT
            for instruction in instructions
        )
        skipped_tool_count = 0
        if redirected:
            state.transition(
                RunStatus.REPLANNING,
                trace_run=trace_run,
                current_step="replanning",
            )
            skipped_tool_count = self._skip_redirected_tool_calls(
                outstanding_tool_calls,
                state,
                trace_run,
                event_emitter,
            )

        applied_at = self._steering_timestamp()
        for instruction in instructions:
            apply_ms = round(
                (time.monotonic() - instruction.received_monotonic) * 1000.0,
                3,
            )
            self.messages.append({
                "role": "user",
                "content": f"{STEERING_MESSAGE_PREFIX}\n{instruction.text}",
            })
            metadata = {
                "instruction_id": instruction.instruction_id,
                "mode": instruction.mode.value,
                "created_at": instruction.created_at,
                "received_state_version": instruction.received_state_version,
                "received_round": instruction.received_round,
                "applied_at": applied_at,
                "safe_point": safe_point,
                "apply_ms": apply_ms,
                "skipped_tool_count": skipped_tool_count,
            }
            state.record_steering(metadata)
            trace_run.emit(
                "steering_applied",
                round=state.round,
                state_version=state.state_version,
                outcome=instruction.mode.value,
                details={
                    "instruction_id": instruction.instruction_id,
                    "mode": instruction.mode.value,
                    "safe_point": safe_point,
                    "apply_ms": apply_ms,
                },
            )
            applied_event = event_emitter.emit(
                AgentEventType.STEERING_APPLIED,
                phase="control",
                message="Steering instruction applied",
                public_data={
                    "instruction_id": instruction.instruction_id,
                    "mode": instruction.mode.value,
                    "safe_point": safe_point,
                    "round": state.round,
                    "apply_ms": apply_ms,
                    "skipped_tool_count": skipped_tool_count,
                },
            )
            if self.run_store is not None:
                self.run_store.mark_steering_applied(
                    state.run_id,
                    instruction.instruction_id,
                    applied_sequence=applied_event.sequence,
                    applied_at=applied_at,
                    safe_point=safe_point,
                    skipped_tool_count=skipped_tool_count,
                    run_status=state.status.value,
                )

        if redirected:
            event_emitter.emit(
                AgentEventType.REPLANNING,
                phase="replanning",
                message="Replanning after user redirect",
                public_data={
                    "instruction_ids": [
                        item.instruction_id for item in instructions
                    ],
                    "safe_point": safe_point,
                    "round": state.round,
                },
            )
            state.transition(
                RunStatus.RUNNING,
                trace_run=trace_run,
                current_step="model_loop",
            )

        if self.run_store is not None:
            self.run_store.record_steering(
                state,
                self.messages,
                instruction_ids=[item.instruction_id for item in instructions],
                modes=[item.mode.value for item in instructions],
                safe_point=safe_point,
                model=getattr(self.llm, "model", None),
                trace_id=trace_run.trace_id,
                trace_ref=self._trace_ref(),
            )
        control_inbox.update_position(state.state_version, state.round)
        return True, redirected

    def _control_checkpoint(
        self,
        control_inbox,
        budget_controller: RunBudgetController,
        cancellation_token: CancellationToken,
        state: RunState,
        trace_run,
        event_emitter: EventEmitter,
        safe_point: str,
        *,
        redirect_only: bool = False,
        close_if_empty: bool = False,
        outstanding_tool_calls=(),
    ) -> tuple[bool, bool]:
        """Apply pause/cancel/steering controls at one non-opaque boundary."""
        if control_inbox is None:
            return False, False
        self._pause_checkpoint(
            control_inbox,
            cancellation_token,
            state,
            trace_run,
            event_emitter,
            safe_point,
        )
        budget_controller.check(f"control:{safe_point}", state, trace_run)
        applied = self._steering_checkpoint(
            control_inbox,
            state,
            trace_run,
            event_emitter,
            safe_point,
            redirect_only=redirect_only,
            close_if_empty=close_if_empty,
            outstanding_tool_calls=outstanding_tool_calls,
        )
        # A callback may request pause while steering events are delivered.
        # The operation has not left this safe point yet, so honor it here.
        self._pause_checkpoint(
            control_inbox,
            cancellation_token,
            state,
            trace_run,
            event_emitter,
            safe_point,
        )
        budget_controller.check(f"control:{safe_point}:applied", state, trace_run)
        return applied

    def _pause_checkpoint(
        self,
        control_inbox,
        cancellation_token: CancellationToken,
        state: RunState,
        trace_run,
        event_emitter: EventEmitter,
        safe_point: str,
    ) -> None:
        """Acknowledge and wait for pause only at an Agent-owned safe point."""
        control_inbox.update_position(state.state_version, state.round)
        paused, has_reason = control_inbox.acknowledge_pause()
        if not paused:
            return
        state.transition(
            RunStatus.PAUSED,
            trace_run=trace_run,
            current_step=f"paused:{safe_point}",
        )
        event_emitter.emit(
            AgentEventType.RUN_PAUSED,
            phase="paused",
            message="Run paused at a safe point",
            public_data={
                "safe_point": safe_point,
                "round": state.round,
                "has_reason": has_reason,
            },
        )
        resumed = control_inbox.wait_until_released(cancellation_token)
        if not resumed:
            return
        state.transition(
            RunStatus.RUNNING,
            trace_run=trace_run,
            current_step="model_loop",
        )
        control_inbox.update_position(state.state_version, state.round)
        event_emitter.emit(
            AgentEventType.RUN_RESUMED,
            phase="running",
            message="Run resumed",
            public_data={"safe_point": safe_point, "round": state.round},
        )

    def _skip_redirected_tool_calls(
        self,
        tool_calls,
        state: RunState,
        trace_run,
        event_emitter: EventEmitter,
    ) -> int:
        pending_ids = {
            item.get("tool_call_id") for item in state.pending_calls
        }
        skipped = 0
        for tc in tool_calls:
            if tc.id not in pending_ids:
                continue
            result = self._failure_result(
                tc,
                "Skipped because a user redirect was received before execution.",
                outcome=ToolOutcome.BLOCKED,
                category="steering_redirect",
                code="STEERING_REDIRECT",
            )
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": (
                    "[Skipped because a user redirect was received before execution.]"
                ),
            })
            self._record_call_completion(
                state,
                tc,
                result,
                trace_run=trace_run,
            )
            trace_run.emit(
                "tool_failed",
                tool_call_id=tc.id,
                tool_name=tc.name,
                round=state.round,
                state_version=state.state_version,
                outcome="redirected",
                error_category="steering_redirect",
            )
            self._emit_tool_terminal(
                event_emitter,
                tc,
                result,
                state.round,
            )
            skipped += 1
        return skipped

    @staticmethod
    def _steering_timestamp() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

    @staticmethod
    def _accepts_keyword(callable_obj, keyword: str) -> bool:
        """Check an extension callable without changing legacy LLM signatures."""
        try:
            parameters = inspect.signature(callable_obj).parameters
        except (TypeError, ValueError):
            return False
        return keyword in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )

    def _trace_ref(self) -> str | None:
        path = getattr(self.trace_sink, "path", None)
        return str(path) if path is not None else None

    def _persist_boundary(self, state: RunState, trace_run, boundary: str) -> None:
        if self.run_store is None:
            return
        self.run_store.save_boundary(
            state,
            self.messages,
            boundary,
            model=getattr(self.llm, "model", None),
            trace_id=trace_run.trace_id,
            trace_ref=self._trace_ref(),
        )

    def _crash_at(self, boundary: str, state: RunState) -> None:
        if self.crash_injector is None:
            return
        self.crash_injector(
            boundary,
            {
                "run_id": state.run_id,
                "round": state.round,
                "state_version": state.state_version,
            },
        )

    @staticmethod
    def _finish_verification(
        state: RunState,
        verification: VerificationResult,
        trace_run,
        model_text: str,
    ) -> None:
        if verification.status is VerificationStatus.PASSED:
            state.transition(
                RunStatus.VERIFIED,
                trace_run=trace_run,
                exit_reason=ExitReason.SUCCESS_CRITERIA_MET,
                final_text=model_text,
            )
        elif verification.status is VerificationStatus.NOT_RUN:
            state.transition(
                RunStatus.UNVERIFIED,
                trace_run=trace_run,
                exit_reason=ExitReason.MODEL_STOPPED_UNVERIFIED,
                final_text=model_text,
            )
        elif verification.status is VerificationStatus.INSUFFICIENT_EVIDENCE:
            state.transition(
                RunStatus.UNVERIFIED,
                trace_run=trace_run,
                exit_reason=ExitReason.INSUFFICIENT_EVIDENCE,
                final_text=model_text,
            )
        elif verification.status is VerificationStatus.FAILED:
            state.transition(
                RunStatus.NEEDS_ACTION,
                trace_run=trace_run,
                exit_reason=ExitReason.VERIFICATION_FAILED,
                final_text=model_text,
            )
        else:
            state.transition(
                RunStatus.FAILED,
                trace_run=trace_run,
                exit_reason=ExitReason.VERIFICATION_ERROR,
                final_text=model_text,
            )

    def _recover_pending_calls(
        self,
        state: RunState,
        trace_run,
        verifier,
        *,
        event_emitter: EventEmitter | None = None,
    ) -> AgentResult | None:
        del verifier  # verification is resumed separately from tool recovery
        if self.run_store is None:
            raise RunStoreError("recovery requires a configured RunStore")
        records = {
            call.tool_call_id: call
            for call in self.run_store.reconcile_started(state.run_id)
        }
        unresolved: list[str] = []
        for pending in list(state.pending_calls):
            call_id = str(pending.get("tool_call_id", ""))
            record = records.get(call_id)
            completed_during_replay = False
            if record is None:
                unresolved.append(f"{call_id or '<missing>'}: no durable tool record")
                continue

            if record.status is ToolCallStatus.SUCCEEDED:
                evidence = tuple((record.result or {}).get("evidence") or ())
                result = ToolResult.success(
                    call_id,
                    record.tool_name,
                    "[recovered] persisted result confirms this tool already succeeded",
                    evidence=evidence,
                )
            elif record.status is ToolCallStatus.FAILED:
                can_retry = (
                    record.idempotent
                    and record.retryable
                    and record.attempt < record.max_attempts
                    and record.replay_arguments is not None
                )
                if can_retry:
                    result = self._resume_tool_call(
                        record,
                        state,
                        trace_run,
                        event_emitter=event_emitter,
                    )
                    completed_during_replay = True
                else:
                    stored = record.result or {}
                    result = ToolResult.failure(
                        call_id,
                        record.tool_name,
                        str(stored.get("safe_message") or "Error: persisted tool call failed."),
                        outcome=ToolOutcome.BUSINESS_ERROR,
                        error_category=str(stored.get("error_category") or "persisted_failure"),
                        error_code=str(stored.get("error_code") or "PERSISTED_FAILURE"),
                        retryable=record.retryable,
                    )
            elif record.status is ToolCallStatus.PLANNED:
                if record.replay_arguments is None:
                    unresolved.append(
                        f"{call_id}: planned arguments were redacted and cannot be replayed"
                    )
                    continue
                result = self._resume_tool_call(
                    record,
                    state,
                    trace_run,
                    event_emitter=event_emitter,
                )
                completed_during_replay = True
            elif (
                record.status is ToolCallStatus.UNKNOWN
                and record.idempotent
                and record.replay_arguments is not None
            ):
                result = self._resume_tool_call(
                    record,
                    state,
                    trace_run,
                    event_emitter=event_emitter,
                )
                completed_during_replay = True
            else:
                unresolved.append(
                    f"{call_id}: {record.tool_name} is {record.status.value}; "
                    "its side effect cannot be confirmed safely"
                )
                continue

            self.messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result.model_content(),
            })
            if not completed_during_replay:
                self._record_call_completion(state, ToolCall(
                    id=call_id,
                    name=record.tool_name,
                    arguments=record.replay_arguments or {},
                ), result, trace_run=trace_run, persist_result=False)

        if unresolved:
            text = (
                "Recovery stopped before replaying an uncertain side effect. "
                "Inspect the external state or confirm a manual retry: "
                + "; ".join(unresolved)
            )
            state.transition(
                RunStatus.NEEDS_ACTION,
                trace_run=trace_run,
                current_step="recovery_needs_action",
                exit_reason=ExitReason.RECOVERY_REQUIRES_ACTION,
                final_text=text,
            )
            self._persist_boundary(state, trace_run, "recovery_needs_action")
            return AgentResult(state=state)

        self._persist_boundary(state, trace_run, "recovery_completed")
        return None

    def _resume_tool_call(
        self,
        record,
        state: RunState,
        trace_run,
        *,
        event_emitter: EventEmitter | None = None,
    ) -> ToolResult:
        call = ToolCall(
            id=record.tool_call_id,
            name=record.tool_name,
            arguments=dict(record.replay_arguments or {}),
        )
        state.transition(
            RunStatus.WAITING_APPROVAL,
            trace_run=trace_run,
            current_step="recovering_tool",
        )
        return self._exec_single_tool(
            call,
            state,
            trace_run,
            state.round,
            event_emitter=event_emitter,
        )

    @staticmethod
    def _normalise_task_spec(task_spec):
        if isinstance(task_spec, str):
            return task_spec, [], None
        if not isinstance(task_spec, Mapping):
            raise TypeError("task_spec must be a goal string or mapping")
        goal = task_spec.get("goal")
        if not isinstance(goal, str):
            raise TypeError("task_spec['goal'] must be a string")
        criteria = task_spec.get("success_criteria", [])
        if isinstance(criteria, str):
            criteria = [criteria]
        if not isinstance(criteria, (list, tuple)) or not all(
            isinstance(item, str) for item in criteria
        ):
            raise TypeError("task_spec['success_criteria'] must contain strings")
        verifier = task_spec.get("verifier")
        if verifier is not None and not (
            callable(verifier) or callable(getattr(verifier, "verify", None))
        ):
            raise TypeError("task_spec['verifier'] must implement verify() or be callable")
        return goal, list(criteria), verifier

    def _verify(
        self,
        verifier,
        state: RunState,
        trace_run,
        *,
        model_text: str,
        event_emitter: EventEmitter | None = None,
    ) -> VerificationResult:
        selected = verifier
        if selected is not None and not callable(getattr(selected, "verify", None)):
            selected = CallableVerifierAdapter(selected)
        verifier_name = getattr(selected, "name", type(selected).__name__) if selected else "none"
        verifier_names = tuple(
            getattr(item, "name", type(item).__name__)
            for item in getattr(selected, "verifiers", ())
        ) or (verifier_name,)
        timer = trace_run.timer()
        trace_run.emit(
            "verification_started",
            timer=timer,
            round=state.round,
            state_version=state.state_version,
            outcome="started",
            details={
                "verifiers": list(verifier_names),
                "criteria_count": len(state.success_criteria),
                "input_evidence": {
                    "reference_sha256": [
                        hashlib.sha256(item.encode("utf-8")).hexdigest()
                        for item in state.evidence_refs
                    ],
                    "completed_call_count": len(state.completed_calls),
                    "pending_call_count": len(state.pending_calls),
                },
            },
        )
        if event_emitter is not None:
            event_emitter.emit(
                AgentEventType.VERIFICATION_STARTED,
                phase="verification",
                message="Verification started",
                public_data={
                    "round": state.round,
                    "verifiers": list(verifier_names),
                    "criteria_count": len(state.success_criteria),
                    "evidence_reference_count": len(state.evidence_refs),
                },
            )
        request = VerificationRequest(
            run_id=state.run_id,
            goal=state.goal,
            success_criteria=tuple(state.success_criteria),
            model_text=model_text,
            completed_calls=tuple(dict(call) for call in state.completed_calls),
            pending_calls=tuple(dict(call) for call in state.pending_calls),
            evidence_refs=tuple(state.evidence_refs),
            state_version=state.state_version,
            command_runner=lambda command, timeout, cwd: self._run_verification_command(
                command,
                timeout,
                cwd,
                trace_run,
                state,
            ),
        )
        if selected is None:
            result = VerificationResult(
                status=VerificationStatus.NOT_RUN,
                verifier="none",
                failed_criteria=tuple(state.success_criteria),
                verifier_names=("none",),
            )
        else:
            try:
                result = selected.verify(request)
                if not isinstance(result, VerificationResult):
                    raise TypeError("Verifier.verify() must return VerificationResult")
            except Exception as exc:  # noqa: BLE001 - verifier is an extension boundary
                result = VerificationResult(
                    status=VerificationStatus.ERROR,
                    verifier=verifier_name,
                    failed_criteria=("verifier completed without an internal error",),
                    verifier_names=verifier_names,
                    error_category=f"verifier_{type(exc).__name__.lower()}",
                )
        trace_run.emit(
            "verification_finished" if result.status is not VerificationStatus.ERROR
            else "verification_failed",
            timer=timer,
            finished=True,
            round=state.round,
            state_version=state.state_version,
            outcome=result.status.value,
            error_category=result.error_category,
            details={
                "verifiers": list(result.verifier_names or (result.verifier,)),
                "verification_status": result.status.value,
                "evidence": [item.trace_dict() for item in result.evidence],
                "failed_criteria_sha256": [
                    hashlib.sha256(item.encode("utf-8")).hexdigest()
                    for item in result.failed_criteria
                ],
            },
        )
        if event_emitter is not None:
            event_emitter.emit(
                AgentEventType.VERIFICATION_FINISHED,
                phase="verification",
                message="Verification finished",
                public_data={
                    "round": state.round,
                    "verification_status": result.status.value,
                    "verifiers": list(
                        result.verifier_names or (result.verifier,)
                    ),
                    "evidence_count": len(result.evidence),
                    "failed_criteria_count": len(result.failed_criteria),
                    "error_category": result.error_category,
                },
            )
        return result

    def _run_verification_command(
        self,
        command: str,
        timeout_seconds: float | None,
        cwd: Path | None,
        trace_run,
        state: RunState,
    ) -> VerificationCommandResult:
        """Execute a caller-configured command through the bash permission gate."""
        arguments = {"command": command}
        bash = self._tool_by_name.get("bash")
        spec = bash.spec if bash is not None else BashTool().spec
        if self.plan_mode:
            refusal = "verification commands are disabled while plan mode is active"
        elif self.permission is None:
            refusal = None
        else:
            refusal = self.permission.check("bash", arguments, spec)
        trace_run.emit(
            "permission_denied" if refusal is not None else "permission_allowed",
            tool_name="verification_command",
            round=state.round,
            state_version=state.state_version,
            outcome="denied" if refusal is not None else "allowed",
            error_category="permission_denied" if refusal is not None else None,
            details={"verification_command": True},
        )
        if refusal is not None:
            return VerificationCommandResult(
                authorized=False,
                error_category="permission_denied",
            )
        execution_cwd = cwd or Path(get_tracked_cwd() or Path.cwd()).resolve()
        try:
            completed = run_shell(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                cwd=str(execution_cwd),
            )
        except subprocess.TimeoutExpired:
            return VerificationCommandResult(
                authorized=True,
                timed_out=True,
                error_category="timeout",
            )
        except Exception:  # noqa: BLE001 - return only a controlled category
            return VerificationCommandResult(
                authorized=True,
                error_category="verification_command_error",
            )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        return VerificationCommandResult(
            authorized=True,
            exit_code=completed.returncode,
            stdout_sha256=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")),
        )

    @staticmethod
    def _failure_exit_reason(state: RunState) -> ExitReason:
        if state.status is RunStatus.VERIFYING:
            return ExitReason.VERIFICATION_ERROR
        if state.current_step == "calling_model":
            return ExitReason.MODEL_ERROR
        if state.status is RunStatus.EXECUTING:
            return ExitReason.TOOL_ERROR
        return ExitReason.INTERNAL_ERROR

    def _exec_single_tool(
        self,
        tc,
        state,
        trace_run,
        round_no: int,
        on_tool=None,
        event_emitter: EventEmitter | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ToolResult:
        if on_tool:
            on_tool(tc.name, tc.arguments)
        refusal = self._pre_hooks(tc)
        if refusal:
            result = self._failure_result(
                tc,
                refusal,
                outcome=ToolOutcome.BLOCKED,
                category="pre_hook_denied",
                code="PRE_HOOK_DENIED",
            )
            trace_run.emit(
                "tool_failed",
                tool_call_id=tc.id,
                tool_name=tc.name,
                round=round_no,
                state_version=state.state_version,
                outcome="blocked",
                error_category=result.error_category,
            )
        else:
            refusal = self._permit_traced(tc, trace_run, round_no, state.state_version)
            result = (
                self._failure_result(
                    tc,
                    refusal,
                    outcome=ToolOutcome.PERMISSION_DENIED,
                    category="permission_denied",
                    code="PERMISSION_DENIED",
                )
                if refusal is not None
                else None
            )
        if result is not None:
            self._emit_tool_terminal(
                event_emitter,
                tc,
                result,
                round_no,
            )
        if result is None:
            state.transition(
                RunStatus.EXECUTING,
                trace_run=trace_run,
                current_step="executing_tools",
            )
            result = self._exec_tool_result_traced(
                tc,
                trace_run,
                round_no,
                state.state_version,
                state=state,
                event_emitter=event_emitter,
                cancellation_token=cancellation_token,
            )
            self._post_hooks(tc, result.model_content())
        self._record_call_completion(state, tc, result, trace_run=trace_run)
        if state.status is not RunStatus.RUNNING:
            state.transition(
                RunStatus.RUNNING,
                trace_run=trace_run,
                current_step="model_loop",
            )
        return result

    @staticmethod
    def _emit_tool_terminal(
        event_emitter: EventEmitter | None,
        tc,
        result: ToolResult,
        round_no: int | None,
    ) -> None:
        if event_emitter is None:
            return
        event_type = (
            AgentEventType.TOOL_FINISHED
            if result.ok
            else AgentEventType.TOOL_FAILED
        )
        event_emitter.emit(
            event_type,
            phase="tools",
            message=(
                f"Tool finished: {tc.name}"
                if result.ok
                else f"Tool failed: {tc.name}"
            ),
            public_data={
                "tool_call_id": tc.id,
                "tool_name": tc.name,
                "round": round_no,
                "outcome": result.outcome.value,
                "error_category": result.error_category,
                "error_code": result.error_code,
                "retryable": result.retryable,
            },
        )

    def _record_call_completion(
        self,
        state: RunState,
        tc,
        result: ToolResult,
        *,
        trace_run=None,
        persist_result: bool = True,
    ) -> None:
        if persist_result and self.run_store is not None:
            self.run_store.mark_tool_result(
                state,
                self.messages,
                result,
                model=getattr(self.llm, "model", None),
                trace_id=trace_run.trace_id if trace_run is not None else None,
                trace_ref=self._trace_ref(),
            )
        state.complete_call(
            tc.id,
            outcome=result.outcome.value,
            error_category=result.error_category,
            evidence_ref=result.evidence[0] if result.evidence else None,
        )

    def _sync_task_progress(self, state: RunState, tc, result: ToolResult) -> None:
        """Derive progress from controller-owned todo state, not model claims."""
        if tc.name != "todo_write" or not result.ok or self._todo is None:
            return
        rendered = self._todo.render()
        markers = [hashlib.sha256(rendered.encode("utf-8")).hexdigest()] if rendered else []
        state.update_task_progress(markers)

    def _queue_exploration_reminder(
        self,
        observation: NoProgressObservation | None,
    ) -> None:
        """Ask the model to summarize and re-plan after repeated exploration."""
        if observation is None:
            return
        if self._pending_exploration_reminder is not None:
            return
        self._pending_exploration_reminder = {
            "tool_name": observation.tool_name,
            "fingerprint": observation.action_fingerprint,
            "count": observation.count,
        }

    def _maybe_compress_traced(self, trace_run, round_no: int) -> bool:
        if not getattr(trace_run.sink, "enabled", True):
            return self.context.maybe_compress(self.messages, self.llm)
        before_tokens = estimate_tokens(self.messages)
        before_messages = len(self.messages)
        timer = trace_run.timer()
        compressed = self.context.maybe_compress(self.messages, self.llm)
        if compressed:
            trace_run.emit(
                "context_compressed",
                timer=timer,
                finished=True,
                round=round_no,
                outcome="compressed",
                details={
                    "before_tokens": before_tokens,
                    "after_tokens": estimate_tokens(self.messages),
                    "before_messages": before_messages,
                    "after_messages": len(self.messages),
                },
            )
        return compressed

    def _permit_traced(
        self,
        tc,
        trace_run,
        round_no: int,
        state_version: int | None = None,
    ) -> str | None:
        result = self._permit(tc)
        trace_run.emit(
            "permission_denied" if result is not None else "permission_allowed",
            tool_call_id=tc.id,
            tool_name=tc.name,
            round=round_no,
            state_version=state_version,
            outcome="denied" if result is not None else "allowed",
            error_category="permission_denied" if result is not None else None,
        )
        return result

    def _build_tool_context(
        self,
        tc,
        round_no: int,
        event_emitter: EventEmitter | None,
        cancellation_token: CancellationToken | None,
    ) -> ToolExecutionContext:
        tool = self._tool_by_name.get(tc.name)
        spec = tool.spec if tool is not None else None
        timeout = spec.timeout_seconds if spec is not None else None
        deadline = time.monotonic() + timeout if timeout is not None else None

        def emit_output(channel: str, text: str) -> None:
            if event_emitter is None:
                return
            event_emitter.emit(
                AgentEventType.TOOL_OUTPUT,
                phase="tools",
                message=f"Tool output: {tc.name}",
                public_data={
                    "tool_call_id": tc.id,
                    "tool_name": tc.name,
                    "round": round_no,
                    "channel": channel,
                    "text": text,
                },
            )

        return ToolExecutionContext(
            run_id=getattr(self, "last_run_id", "") or "",
            tool_call_id=tc.id,
            tool_name=tc.name,
            deadline=deadline,
            cancellation_probe=(
                lambda: cancellation_token.is_cancelled
                if cancellation_token is not None
                else False
            ),
            output_sink=emit_output,
        )

    def _exec_tool_result_traced(
        self,
        tc,
        trace_run,
        round_no: int,
        state_version: int | None = None,
        *,
        state: RunState | None = None,
        event_emitter: EventEmitter | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ToolResult:
        if self.run_store is not None and state is not None:
            self.run_store.mark_tool_started(
                state,
                self.messages,
                tc.id,
                model=getattr(self.llm, "model", None),
                trace_id=trace_run.trace_id,
                trace_ref=self._trace_ref(),
            )
            self._crash_at("tool_before_execute", state)
        timer = trace_run.timer()
        trace_run.emit(
            "tool_started",
            timer=timer,
            tool_call_id=tc.id,
            tool_name=tc.name,
            round=round_no,
            state_version=state_version,
            outcome="started",
        )
        if event_emitter is not None:
            event_emitter.emit(
                AgentEventType.TOOL_STARTED,
                phase="tools",
                message=f"Tool started: {tc.name}",
                public_data={
                    "tool_call_id": tc.id,
                    **safe_tool_summary(tc.name, tc.arguments),
                    "round": round_no,
                },
            )
        try:
            context = self._build_tool_context(
                tc,
                round_no,
                event_emitter,
                cancellation_token,
            )
            result = self._exec_tool_result(
                tc,
                context,
                round_no=round_no,
                event_emitter=event_emitter,
                trace_run=trace_run,
                state_version=state_version,
            )
        except BaseException as exc:
            trace_run.emit(
                "tool_failed",
                timer=timer,
                finished=True,
                tool_call_id=tc.id,
                tool_name=tc.name,
                round=round_no,
                state_version=state_version,
                outcome="failed",
                error_category=type(exc).__name__,
            )
            if event_emitter is not None:
                event_emitter.emit(
                    AgentEventType.TOOL_FAILED,
                    phase="tools",
                    message=f"Tool failed: {tc.name}",
                    public_data={
                        "tool_call_id": tc.id,
                        "tool_name": tc.name,
                        "round": round_no,
                        "error_category": type(exc).__name__,
                    },
                )
            raise
        if self.run_store is not None and state is not None:
            self._crash_at("tool_after_execute_before_receipt", state)
            self.run_store.mark_tool_result(
                state,
                self.messages,
                result,
                model=getattr(self.llm, "model", None),
                trace_id=trace_run.trace_id,
                trace_ref=self._trace_ref(),
            )
            self._crash_at("tool_after_result_persisted_before_receipt", state)
        trace_run.emit(
            "tool_finished" if result.ok else "tool_failed",
            timer=timer,
            finished=True,
            tool_call_id=tc.id,
            tool_name=tc.name,
            round=round_no,
            state_version=state_version,
            outcome="success" if result.ok else "failed",
            error_category=result.error_category,
            details={
                "tool_outcome": result.outcome.value,
                "error_code": result.error_code,
                "retryable": result.retryable,
            },
        )
        self._emit_tool_terminal(event_emitter, tc, result, round_no)
        return result

    def _failure_result(
        self,
        tc,
        safe_message: str,
        *,
        outcome: ToolOutcome,
        category: str,
        code: str,
    ) -> ToolResult:
        result = ToolResult.failure(
            tc.id,
            tc.name,
            safe_message,
            outcome=outcome,
            error_category=category,
            error_code=code,
        )
        self.tool_results.append(result)
        return result

    def _pre_hooks(self, tc) -> str | None:
        """PreToolUse hooks, fired before consent. A string return blocks the
        call and becomes the tool result the model sees; None lets it through."""
        if self.hooks is None:
            return None
        return self.hooks.run_pre(tc.name, tc.arguments)

    def _post_hooks(self, tc, result: str):
        """PostToolUse hooks observe a finished call; they can never block."""
        if self.hooks is not None:
            self.hooks.run_post(tc.name, tc.arguments, result)

    def _permit(self, tc) -> str | None:
        """Consent check for one call. None means go ahead; a string is the
        refusal, returned as the tool result instead of executing."""
        tool = self._tool_by_name.get(tc.name)
        tool_spec = tool.spec if tool is not None else None
        # plan mode outranks consent, even --yes: while it's on nothing mutates
        if self.plan_mode and (
            tool_spec is None or tool_spec.requires_confirmation
        ):
            return (
                "Plan mode is on, so this call was refused: plan mode is "
                "read-only. Do not retry it. Keep investigating with the "
                "read-only tools, then present the plan and stop. The user can "
                'approve it by typing "approve", or exit plan mode with /plan.'
            )
        if self.permission is None:
            return None
        return self.permission.check(tc.name, tc.arguments, tool_spec)

    def _calls_parallel_safe(self, tool_calls) -> bool:
        """Compatibility helper backed by the scheduler's complete policy."""
        return self.tool_scheduler.plan(tool_calls).parallel

    def _exec_tool(self, tc) -> str:
        """Compatibility wrapper returning the model-safe string."""
        return self._exec_tool_result(tc).model_content()

    def invalidate_tool_cache(self, reason: str = "manual") -> int:
        """Explicitly invalidate cached read-only results and bump revision."""
        return self.tool_cache.invalidate(reason)

    def _emit_cache_hit(
        self,
        tc,
        round_no: int | None,
        event_emitter: EventEmitter | None,
        trace_run=None,
        state_version: int | None = None,
    ) -> None:
        if event_emitter is not None:
            event_emitter.emit(
                AgentEventType.CACHE_HIT,
                phase="tools",
                message=f"Tool result cache hit: {tc.name}",
                public_data={
                    "tool_call_id": tc.id,
                    "tool_name": tc.name,
                    "round": round_no,
                },
            )
        if trace_run is not None:
            trace_run.emit(
                "cache_hit",
                tool_call_id=tc.id,
                tool_name=tc.name,
                round=round_no,
                state_version=state_version,
                outcome="hit",
            )

    def _exec_tool_result(
        self,
        tc,
        context: ToolExecutionContext | None = None,
        *,
        round_no: int | None = None,
        event_emitter: EventEmitter | None = None,
        trace_run=None,
        state_version: int | None = None,
    ) -> ToolResult:
        """Execute one call through the structured protocol boundary."""
        adapter = self._tool_adapters.get(tc.name)
        if adapter is None:
            return self._failure_result(
                tc,
                f"Error: unknown tool '{tc.name}'",
                outcome=ToolOutcome.VALIDATION_ERROR,
                category="unknown_tool",
                code="UNKNOWN_TOOL",
            )
        spec = adapter.spec
        # Any call that may mutate the workspace/external state invalidates
        # every cached read before it starts.  Conservative by design.
        if spec.side_effect in {"write", "external"}:
            self.tool_cache.invalidate(f"tool:{spec.name}")

        if self.tool_cache.is_cacheable(spec):
            cache_hit, result, _key = self.tool_cache.get_or_execute(
                spec,
                tc.arguments,
                call_id=tc.id,
                cwd=os.getcwd(),
                execute=lambda: adapter.invoke(tc.id, tc.arguments, context),
            )
            if cache_hit:
                self._emit_cache_hit(
                    tc,
                    round_no,
                    event_emitter,
                    trace_run,
                    state_version,
                )
            self.tool_results.append(result)
            return result

        result = adapter.invoke(tc.id, tc.arguments, context)
        self.tool_results.append(result)
        return result

    def _exec_tool_results_parallel(
        self,
        tool_calls,
        on_tool=None,
        trace_run=None,
        round_no: int | None = None,
        state: RunState | None = None,
        schedule: SchedulePlan | None = None,
        event_emitter: EventEmitter | None = None,
        cancellation_probe=None,
        cancellation_token: CancellationToken | None = None,
    ) -> list[ToolResult]:
        """Compatibility entry point delegated to :class:`ToolScheduler`."""
        calls = tuple(tool_calls)
        for tc in calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)

        def prepare(tc):
            refusal = self._pre_hooks(tc)
            if refusal:
                result = self._failure_result(
                    tc,
                    refusal,
                    outcome=ToolOutcome.BLOCKED,
                    category="pre_hook_denied",
                    code="PRE_HOOK_DENIED",
                )
                if trace_run is not None:
                    trace_run.emit(
                        "tool_failed",
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        round=round_no,
                        state_version=state.state_version if state is not None else None,
                        outcome="blocked",
                        error_category=result.error_category,
                    )
                self._emit_tool_terminal(event_emitter, tc, result, round_no)
                return result
            refusal = (
                self._permit_traced(
                    tc,
                    trace_run,
                    round_no,
                    state.state_version if state is not None else None,
                )
                if trace_run is not None
                else self._permit(tc)
            )
            if refusal is None:
                return None
            result = self._failure_result(
                tc,
                refusal,
                outcome=ToolOutcome.PERMISSION_DENIED,
                category="permission_denied",
                code="PERMISSION_DENIED",
            )
            self._emit_tool_terminal(event_emitter, tc, result, round_no)
            return result

        def invoke(tc):
            if trace_run is None:
                if event_emitter is not None:
                    event_emitter.emit(
                        AgentEventType.TOOL_STARTED,
                        phase="tools",
                        message=f"Tool started: {tc.name}",
                        public_data={
                            "tool_call_id": tc.id,
                            **safe_tool_summary(tc.name, tc.arguments),
                            "round": round_no,
                        },
                    )
                result = self._exec_tool_result(
                    tc,
                    self._build_tool_context(
                        tc,
                        round_no,
                        event_emitter,
                        cancellation_token,
                    ),
                    round_no=round_no,
                    event_emitter=event_emitter,
                    state_version=state.state_version if state is not None else None,
                )
                self._emit_tool_terminal(event_emitter, tc, result, round_no)
                return result
            return self._exec_tool_result_traced(
                tc,
                trace_run,
                round_no,
                state.state_version if state is not None else None,
                state=state,
                event_emitter=event_emitter,
                cancellation_token=cancellation_token,
            )

        def before_execution():
            if state is not None:
                state.transition(
                    RunStatus.EXECUTING,
                    trace_run=trace_run,
                    current_step="executing_tools",
                )

        def make_failure(tc, message, outcome, category, code):
            return self._failure_result(
                tc,
                message,
                outcome=outcome,
                category=category,
                code=code,
            )

        selected_schedule = schedule or self.tool_scheduler.plan(calls)
        if schedule is None:
            self.tool_scheduler.record_decision(
                selected_schedule,
                trace_run,
                round_no=round_no,
                state_version=state.state_version if state is not None else None,
            )
        try:
            results = self.tool_scheduler.execute(
                calls,
                invoke,
                prepare=prepare,
                after=lambda tc, result: self._post_hooks(tc, result.model_content()),
                before_execution=before_execution,
                failure_factory=make_failure,
                cancellation_probe=cancellation_probe,
                plan=selected_schedule,
                trace_run=trace_run,
                round_no=round_no,
                state_version=state.state_version if state is not None else None,
            )
        except SchedulerInterrupted as interruption:
            results = interruption.results
            if isinstance(interruption.cause, InjectedProcessCrash):
                # A real process loss cannot backfill in-memory state. Durable
                # per-call rows already distinguish SUCCEEDED, STARTED and
                # PLANNED, so leave the batch pending for conservative resume.
                raise interruption.cause
            if state is not None:
                for tc, result in zip(calls, results):
                    self._record_call_completion(
                        state,
                        tc,
                        result,
                        trace_run=trace_run,
                    )
                state.transition(
                    RunStatus.RUNNING,
                    trace_run=trace_run,
                    current_step="model_loop",
                )
            if isinstance(interruption.cause, SchedulerCancelled):
                for tc, result in zip(calls, results):
                    if result.error_category == "scheduler_cancelled":
                        self._emit_tool_terminal(
                            event_emitter,
                            tc,
                            result,
                            round_no,
                        )
                return results
            raise interruption.cause
        if state is not None:
            for tc, result in zip(calls, results):
                self._record_call_completion(
                    state,
                    tc,
                    result,
                    trace_run=trace_run,
                )
            state.transition(
                RunStatus.RUNNING,
                trace_run=trace_run,
                current_step="model_loop",
            )
        return results

    def _exec_tools_parallel(
        self,
        tool_calls,
        on_tool=None,
        trace_run=None,
        round_no: int | None = None,
        state: RunState | None = None,
    ) -> list[str]:
        """Compatibility wrapper for callers expecting result strings."""
        return [
            result.model_content()
            for result in self._exec_tool_results_parallel(
                tool_calls,
                on_tool,
                trace_run=trace_run,
                round_no=round_no,
                state=state,
            )
        ]

    def _finish_pending_tool_calls(
        self,
        tool_calls,
        state: RunState,
        trace_run,
        content: str,
        outcome: str,
        *,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        if not tool_calls:
            return
        self._answer_pending_tool_calls(tool_calls, content=content)
        pending_ids = {call.get("tool_call_id") for call in state.pending_calls}
        for tc in tool_calls:
            if tc.id not in pending_ids:
                continue
            state.complete_call(tc.id, outcome=outcome, error_category=outcome)
            trace_run.emit(
                "tool_failed",
                tool_call_id=tc.id,
                tool_name=tc.name,
                round=state.round,
                state_version=state.state_version,
                outcome=outcome,
                error_category=outcome,
            )
            if event_emitter is not None:
                event_emitter.emit(
                    AgentEventType.TOOL_FAILED,
                    phase="tools",
                    message=f"Tool did not finish: {tc.name}",
                    public_data={
                        "tool_call_id": tc.id,
                        "tool_name": tc.name,
                        "round": state.round,
                        "outcome": outcome,
                        "error_category": outcome,
                    },
                )

    def _answer_pending_tool_calls(self, tool_calls, content: str = "[interrupted]"):
        """Backfill a tool reply for every call that didn't get one.

        OpenAI-compatible APIs reject a request where an assistant message has
        tool_calls without a matching tool reply for each id, so this keeps the
        history valid when execution is interrupted partway through.
        """
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for tc in tool_calls:
            if tc.id not in answered:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                })

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
        self.tool_results.clear()
        # a user-visible reset must not leak the dead conversation's checklist
        # into the next system prompt
        todo = self._tool_by_name.get("todo_write")
        if isinstance(todo, TodoWriteTool):
            todo._tasks = []
