"""Metadata-driven, fail-safe scheduling for tool-call batches."""

from __future__ import annotations

import concurrent.futures
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .tools.protocol import ToolOutcome, ToolResult


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    """An immutable scheduling decision for one model-produced batch."""

    tool_calls: tuple[Any, ...]
    parallel: bool
    reasons: tuple[str, ...]
    resource_keys: tuple[tuple[str, ...], ...]
    dependencies: tuple[tuple[str, ...], ...]
    conflict_count: int = 0


class SchedulerCancelled(RuntimeError):
    """Internal marker used when a caller-provided cancellation probe fires."""


class SchedulerInterrupted(BaseException):
    """Carries legal results for every call when execution is interrupted."""

    def __init__(self, cause: BaseException, results: list[ToolResult]):
        self.cause = cause
        self.results = results
        super().__init__(str(cause))


FailureFactory = Callable[
    [Any, str, ToolOutcome, str, str],
    ToolResult,
]


class ToolScheduler:
    """Plan and execute batches using runtime-only :class:`ToolSpec` data.

    The scheduler never inspects tool names. Tools describe resources and data
    dependencies through optional hooks; governance comes exclusively from
    ``tool.spec``. Results are always returned in the model's original order.
    """

    def __init__(self, tools: Mapping[str, Any] | Iterable[Any], *, max_workers: int = 8):
        if isinstance(tools, Mapping):
            self._tools = dict(tools)
        else:
            self._tools = {tool.name: tool for tool in tools}
        self.max_workers = max(1, max_workers)

    def plan(
        self,
        tool_calls: Iterable[Any],
        *,
        dependencies: Mapping[str, Collection[str]] | None = None,
        resolved_call_ids: Collection[str] = (),
    ) -> SchedulePlan:
        calls = tuple(tool_calls)
        resolved = frozenset(resolved_call_ids)
        resources: list[tuple[str, ...]] = []
        call_dependencies: list[tuple[str, ...]] = []
        specs: list[Any | None] = []
        reasons: list[str] = []

        for call in calls:
            tool = self._tools.get(call.name)
            spec = tool.spec if tool is not None else None
            specs.append(spec)
            if spec is None:
                reasons.append(f"unknown_tool:{call.name}")
                resources.append(())
                call_dependencies.append(())
                continue

            if not spec.parallel_safe:
                reasons.append(f"parallel_unsafe:{call.name}")
            if spec.requires_confirmation:
                reasons.append(f"interactive_approval:{call.name}")
            if spec.side_effect in {"write", "external"}:
                reasons.append(f"side_effect_{spec.side_effect}:{call.name}")
            if spec.risk == "high":
                reasons.append(f"high_risk:{call.name}")

            try:
                keys = tuple(dict.fromkeys(str(key) for key in tool.resource_keys(call.arguments)))
            except Exception:  # noqa: BLE001 - unsafe metadata fails closed
                keys = ()
                reasons.append(f"resource_resolution_failed:{call.name}")
            resources.append(keys)

            declared = set(getattr(call, "depends_on", ()) or ())
            if dependencies is not None:
                declared.update(dependencies.get(call.id, ()))
            try:
                declared.update(tool.data_dependencies(call.arguments))
            except Exception:  # noqa: BLE001 - unsafe metadata fails closed
                reasons.append(f"dependency_resolution_failed:{call.name}")
            unresolved = tuple(sorted(str(item) for item in declared if item not in resolved))
            call_dependencies.append(unresolved)
            if unresolved:
                reasons.append(f"unresolved_dependencies:{call.name}")

        conflict_count = self._resource_conflict_count(resources, specs)
        if conflict_count:
            reasons.append("resource_conflict")
        if len(calls) < 2:
            reasons.append("single_call")

        unique_reasons = tuple(dict.fromkeys(reasons))
        return SchedulePlan(
            tool_calls=calls,
            parallel=len(calls) > 1 and not unique_reasons,
            reasons=unique_reasons or ("all_parallel_requirements_satisfied",),
            resource_keys=tuple(resources),
            dependencies=tuple(call_dependencies),
            conflict_count=conflict_count,
        )

    @staticmethod
    def _resource_conflict_count(resources, specs) -> int:
        """Count incompatible resource users; read/read sharing is safe."""
        users: dict[str, list[int]] = {}
        for index, keys in enumerate(resources):
            for key in keys:
                users.setdefault(key, []).append(index)
        conflicts = 0
        for indexes in users.values():
            if len(indexes) < 2:
                continue
            if all(
                specs[index] is not None
                and specs[index].side_effect in {"none", "read"}
                for index in indexes
            ):
                continue
            conflicts += len(indexes) - 1
        return conflicts

    def record_decision(
        self,
        plan: SchedulePlan,
        trace_run,
        *,
        round_no: int | None = None,
        state_version: int | None = None,
    ) -> None:
        if trace_run is None:
            return
        reason_counts = Counter(reason.split(":", 1)[0] for reason in plan.reasons)
        trace_run.emit(
            "schedule_decided",
            round=round_no,
            state_version=state_version,
            outcome="parallel" if plan.parallel else "serial",
            details={
                "mode": "parallel" if plan.parallel else "serial",
                "call_count": len(plan.tool_calls),
                "reason_counts": dict(sorted(reason_counts.items())),
                "reasons": list(plan.reasons),
                "resource_conflict_count": plan.conflict_count,
                "unresolved_dependency_count": sum(bool(item) for item in plan.dependencies),
            },
        )

    @staticmethod
    def record_queue_wait(
        call,
        trace_run,
        timer,
        *,
        parallel: bool,
        round_no: int | None = None,
        state_version: int | None = None,
    ) -> None:
        if trace_run is None or timer is None:
            return
        trace_run.emit(
            "tool_queue_finished",
            timer=timer,
            finished=True,
            tool_call_id=call.id,
            tool_name=call.name,
            round=round_no,
            state_version=state_version,
            outcome="ready",
            details={"schedule_mode": "parallel" if parallel else "serial"},
        )

    def execute(
        self,
        tool_calls: Iterable[Any],
        invoke: Callable[[Any], ToolResult],
        *,
        prepare: Callable[[Any], ToolResult | None] | None = None,
        after: Callable[[Any, ToolResult], None] | None = None,
        before_execution: Callable[[], None] | None = None,
        failure_factory: FailureFactory | None = None,
        cancellation_probe: Callable[[], bool] | None = None,
        plan: SchedulePlan | None = None,
        trace_run=None,
        round_no: int | None = None,
        state_version: int | None = None,
    ) -> list[ToolResult]:
        calls = tuple(tool_calls)
        selected_plan = plan or self.plan(calls)
        if selected_plan.tool_calls != calls:
            raise ValueError("schedule plan does not match tool calls")
        if plan is None:
            self.record_decision(
                selected_plan,
                trace_run,
                round_no=round_no,
                state_version=state_version,
            )
        make_failure = failure_factory or self._default_failure
        results: list[ToolResult | None] = [None] * len(calls)
        runnable: list[int] = []
        interrupted: BaseException | None = None

        for index, call in enumerate(calls):
            try:
                result = prepare(call) if prepare is not None else None
            except Exception:  # noqa: BLE001 - callback failures are isolated
                result = make_failure(
                    call,
                    "Error: tool preparation failed safely.",
                    ToolOutcome.UNKNOWN_ERROR,
                    "scheduler_prepare_error",
                    "SCHEDULER_PREPARE_ERROR",
                )
            except BaseException as exc:  # noqa: BLE001 - fill every slot before propagating
                interrupted = exc
                break
            if result is None:
                runnable.append(index)
            else:
                results[index] = result

        if interrupted is None and runnable and self._is_cancelled(cancellation_probe):
            interrupted = SchedulerCancelled("tool scheduling was cancelled")

        if interrupted is None and runnable:
            try:
                if before_execution is not None:
                    before_execution()
                if selected_plan.parallel:
                    interrupted = self._execute_parallel(
                        calls,
                        runnable,
                        results,
                        invoke,
                        after,
                        make_failure,
                        cancellation_probe,
                        trace_run,
                        round_no,
                        state_version,
                    )
                else:
                    interrupted = self._execute_serial(
                        calls,
                        runnable,
                        results,
                        invoke,
                        after,
                        make_failure,
                        cancellation_probe,
                        trace_run,
                        round_no,
                        state_version,
                    )
            except BaseException as exc:  # noqa: BLE001 - never leave a missing reply
                interrupted = exc

        if interrupted is not None:
            for index, call in enumerate(calls):
                if results[index] is None:
                    results[index] = make_failure(
                        call,
                        "Error: tool call was cancelled before completion.",
                        ToolOutcome.BLOCKED,
                        "scheduler_cancelled",
                        "SCHEDULER_CANCELLED",
                    )
            raise SchedulerInterrupted(interrupted, self._complete_results(results))
        return self._complete_results(results)

    def _execute_parallel(
        self,
        calls,
        runnable,
        results,
        invoke,
        after,
        make_failure,
        cancellation_probe,
        trace_run,
        round_no,
        state_version,
    ) -> BaseException | None:
        queue_timers = {
            index: trace_run.timer() if trace_run is not None else None for index in runnable
        }

        def run(index: int):
            call = calls[index]
            self.record_queue_wait(
                call,
                trace_run,
                queue_timers[index],
                parallel=True,
                round_no=round_no,
                state_version=state_version,
            )
            if self._is_cancelled(cancellation_probe):
                raise SchedulerCancelled("tool scheduling was cancelled")
            execution_timer = trace_run.timer() if trace_run is not None else None
            try:
                result = invoke(call)
            except Exception:  # noqa: BLE001 - hide callback internals from the model
                result = make_failure(
                    call,
                    "Error: tool execution failed safely.",
                    ToolOutcome.UNKNOWN_ERROR,
                    "scheduler_execution_error",
                    "SCHEDULER_EXECUTION_ERROR",
                )
            except BaseException as exc:  # noqa: BLE001 - returned to the caller thread
                self._record_execution(
                    call,
                    None,
                    trace_run,
                    execution_timer,
                    round_no,
                    state_version,
                    error_category=type(exc).__name__,
                )
                return None, exc
            self._record_execution(
                call,
                result,
                trace_run,
                execution_timer,
                round_no,
                state_version,
            )
            return result, None

        interrupted: BaseException | None = None
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(runnable)),
            thread_name_prefix="corecoder-tool",
        ) as pool:
            futures = {index: pool.submit(run, index) for index in runnable}
            for index, future in futures.items():
                try:
                    result, exc = future.result()
                except BaseException as future_exc:  # noqa: BLE001 - executor failure
                    result, exc = None, future_exc
                if result is not None:
                    results[index] = result
                    self._run_after(after, calls[index], result)
                if exc is not None and interrupted is None:
                    interrupted = exc
        return interrupted

    def _execute_serial(
        self,
        calls,
        runnable,
        results,
        invoke,
        after,
        make_failure,
        cancellation_probe,
        trace_run,
        round_no,
        state_version,
    ) -> BaseException | None:
        for index in runnable:
            call = calls[index]
            queue_timer = trace_run.timer() if trace_run is not None else None
            self.record_queue_wait(
                call,
                trace_run,
                queue_timer,
                parallel=False,
                round_no=round_no,
                state_version=state_version,
            )
            if self._is_cancelled(cancellation_probe):
                return SchedulerCancelled("tool scheduling was cancelled")
            execution_timer = trace_run.timer() if trace_run is not None else None
            try:
                result = invoke(call)
            except Exception:  # noqa: BLE001 - hide callback internals from the model
                result = make_failure(
                    call,
                    "Error: tool execution failed safely.",
                    ToolOutcome.UNKNOWN_ERROR,
                    "scheduler_execution_error",
                    "SCHEDULER_EXECUTION_ERROR",
                )
            except BaseException as exc:  # noqa: BLE001 - propagate after backfill
                self._record_execution(
                    call,
                    None,
                    trace_run,
                    execution_timer,
                    round_no,
                    state_version,
                    error_category=type(exc).__name__,
                )
                return exc
            self._record_execution(
                call,
                result,
                trace_run,
                execution_timer,
                round_no,
                state_version,
            )
            results[index] = result
            self._run_after(after, call, result)
        return None

    @staticmethod
    def _record_execution(
        call,
        result: ToolResult | None,
        trace_run,
        timer,
        round_no,
        state_version,
        *,
        error_category: str | None = None,
    ) -> None:
        if trace_run is None or timer is None:
            return
        trace_run.emit(
            "tool_execution_finished",
            timer=timer,
            finished=True,
            tool_call_id=call.id,
            tool_name=call.name,
            round=round_no,
            state_version=state_version,
            outcome="success" if result is not None and result.ok else "failed",
            error_category=(
                error_category
                if error_category is not None
                else result.error_category if result is not None else "scheduler_error"
            ),
            details={
                "tool_outcome": result.outcome.value if result is not None else "interrupted"
            },
        )

    @staticmethod
    def _run_after(after, call, result) -> None:
        if after is None:
            return
        try:
            after(call, result)
        except Exception:  # noqa: BLE001 - observer callbacks fail open
            return

    @staticmethod
    def _is_cancelled(probe: Callable[[], bool] | None) -> bool:
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - a broken probe fails closed
            return True

    @staticmethod
    def _complete_results(results: list[ToolResult | None]) -> list[ToolResult]:
        if any(result is None for result in results):
            raise RuntimeError("scheduler invariant violated: missing tool result")
        return [result for result in results if result is not None]

    @staticmethod
    def _default_failure(
        call,
        safe_message: str,
        outcome: ToolOutcome,
        category: str,
        code: str,
    ) -> ToolResult:
        return ToolResult.failure(
            call.id,
            call.name,
            safe_message,
            outcome=outcome,
            error_category=category,
            error_code=code,
        )
