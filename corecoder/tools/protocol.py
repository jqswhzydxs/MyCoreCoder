"""Structured tool contracts shared by the model boundary and runtime."""

from __future__ import annotations

import inspect
import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Literal


class ToolOutcome(str, Enum):
    SUCCESS = "success"
    BUSINESS_ERROR = "business_error"
    VALIDATION_ERROR = "validation_error"
    PERMISSION_DENIED = "permission_denied"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN_ERROR = "unknown_error"


class ToolExecutionContext:
    """Runtime-only capabilities offered to a context-aware tool.

    The context is deliberately not part of ``ToolSpec.function_schema``: the
    model only ever sees the tool's declared arguments.  Tools that do not opt
    in keep the legacy ``execute(**arguments)`` contract unchanged.
    """

    __slots__ = (
        "_cancellation_probe",
        "_output_sink",
        "deadline",
        "run_id",
        "tool_call_id",
        "tool_name",
    )

    def __init__(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        deadline: float | None = None,
        cancellation_probe: Callable[[], bool] | None = None,
        output_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        self.run_id = str(run_id)
        self.tool_call_id = str(tool_call_id)
        self.tool_name = str(tool_name)
        self.deadline = deadline
        self._cancellation_probe = cancellation_probe or (lambda: False)
        self._output_sink = output_sink

    def cancellation_probe(self) -> bool:
        """Return True when the owning run has asked this call to stop."""
        try:
            return bool(self._cancellation_probe())
        except Exception:  # noqa: BLE001 - a broken probe must not crash a tool
            return False

    def remaining_timeout(self) -> float | None:
        """Seconds left before the run's deadline, or None when unbounded."""
        if self.deadline is None:
            return None
        return max(0.0, float(self.deadline) - time.monotonic())

    def emit_output(self, channel: str, text: str) -> None:
        """Publish one stdout/stderr chunk to the runtime's observation sink."""
        if channel not in {"stdout", "stderr"}:
            raise ValueError("tool output channel must be 'stdout' or 'stderr'")
        if not text or self._output_sink is None:
            return
        try:
            self._output_sink(channel, text)
        except Exception:  # noqa: BLE001 - observation is best effort
            return



class ToolError(RuntimeError):
    """Expected tool failure with a model-safe message and runtime metadata."""

    def __init__(
        self,
        safe_message: str,
        *,
        category: str = "tool_error",
        code: str = "TOOL_ERROR",
        retryable: bool = False,
        outcome: ToolOutcome = ToolOutcome.BUSINESS_ERROR,
        evidence: tuple[str, ...] = (),
        debug_message: str | None = None,
    ):
        self.safe_message = safe_message
        self.category = category
        self.code = code
        self.retryable = retryable
        self.outcome = outcome
        self.evidence = evidence
        super().__init__(debug_message or safe_message)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Public function contract plus runtime-only governance metadata."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect: Literal["none", "read", "write", "external"] = "external"
    risk: Literal["low", "medium", "high"] = "high"
    required_permissions: tuple[str, ...] = ("tool:execute",)
    requires_confirmation: bool = True
    idempotent: bool = False
    parallel_safe: bool = False
    timeout_seconds: float | None = None
    retry_policy: dict[str, Any] = field(
        default_factory=lambda: {"max_attempts": 1, "backoff_seconds": 0}
    )
    redact_fields: tuple[str, ...] = ()
    version: str = "1"
    # A tool may opt out of the read-only result cache even when its side
    # effect is ``none``/``read`` (for example a live query endpoint).
    cacheable: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolSpec.name must not be empty")
        if self.side_effect not in {"none", "read", "write", "external"}:
            raise ValueError(f"unsupported side_effect: {self.side_effect}")
        if self.risk not in {"low", "medium", "high"}:
            raise ValueError(f"unsupported risk: {self.risk}")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None")

    def function_schema(self) -> dict[str, Any]:
        """The only part exposed to the model's Function Calling API."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def runtime_metadata(self) -> dict[str, Any]:
        """Governance fields for permission, scheduling, tracing and retries."""
        return {
            "side_effect": self.side_effect,
            "risk": self.risk,
            "required_permissions": list(self.required_permissions),
            "requires_confirmation": self.requires_confirmation,
            "idempotent": self.idempotent,
            "parallel_safe": self.parallel_safe,
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": dict(self.retry_policy),
            "redact_fields": list(self.redact_fields),
            "version": self.version,
            "cacheable": self.cacheable,
            "output_schema": self.output_schema,
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    outcome: ToolOutcome
    call_id: str
    tool_name: str
    data: Any = None
    safe_message: str | None = None
    error_category: str | None = None
    error_code: str | None = None
    retryable: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.ok != (self.outcome is ToolOutcome.SUCCESS):
            raise ValueError("ToolResult.ok must match a success outcome")
        if not self.ok and not self.safe_message:
            raise ValueError("failed ToolResult requires safe_message")

    @classmethod
    def success(
        cls,
        call_id: str,
        tool_name: str,
        data: Any,
        *,
        evidence: tuple[str, ...] = (),
    ) -> ToolResult:
        return cls(
            ok=True,
            outcome=ToolOutcome.SUCCESS,
            call_id=call_id,
            tool_name=tool_name,
            data=data,
            evidence=evidence,
        )

    @classmethod
    def failure(
        cls,
        call_id: str,
        tool_name: str,
        safe_message: str,
        *,
        outcome: ToolOutcome,
        error_category: str,
        error_code: str,
        retryable: bool = False,
        evidence: tuple[str, ...] = (),
    ) -> ToolResult:
        return cls(
            ok=False,
            outcome=outcome,
            call_id=call_id,
            tool_name=tool_name,
            safe_message=safe_message,
            error_category=error_category,
            error_code=error_code,
            retryable=retryable,
            evidence=evidence,
        )

    def model_content(self) -> str:
        """Render only model-safe content; runtime fields remain private."""
        if not self.ok:
            return self.safe_message or "Error: tool call failed safely."
        if isinstance(self.data, str):
            return self.data
        return json.dumps(self.data, ensure_ascii=False, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "outcome": self.outcome.value,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "data": self.data,
            "safe_message": self.safe_message,
            "error_category": self.error_category,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "evidence": list(self.evidence),
        }


class _SchemaValidationError(ValueError):
    pass


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema(value: Any, schema: Any, path: str = "$") -> None:
    """Validate the JSON-Schema subset used by built-in and MCP tools."""
    if schema is True or not schema:
        return
    if schema is False:
        raise _SchemaValidationError(f"{path} is not allowed")
    if not isinstance(schema, dict):
        raise _SchemaValidationError(f"{path} has an invalid schema")

    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_json_type_matches(value, item) for item in expected):
            raise _SchemaValidationError(f"{path} must have type {' or '.join(expected)}")
    elif isinstance(expected, str) and not _json_type_matches(value, expected):
        raise _SchemaValidationError(f"{path} must have type {expected}")

    if "enum" in schema and value not in schema["enum"]:
        raise _SchemaValidationError(f"{path} must be one of {schema['enum']!r}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                raise _SchemaValidationError(f"{path}.{name} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise _SchemaValidationError(f"{path} has unexpected field {unexpected[0]!r}")
        for name, item in value.items():
            if name in properties:
                _validate_schema(item, properties[name], f"{path}.{name}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], f"{path}[{index}]")


class LegacyToolAdapter:
    """Run old string-returning tools behind the structured protocol."""

    def __init__(self, tool):
        self.tool = tool

    @property
    def spec(self) -> ToolSpec:
        return self.tool.spec

    def invoke(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            _validate_schema(arguments, self.spec.input_schema)
        except _SchemaValidationError as exc:
            return self._validation_failure(call_id, f"schema validation failed: {exc}")
        except (AttributeError, TypeError, ValueError):
            return ToolResult.failure(
                call_id,
                self.spec.name,
                f"Error: {self.spec.name} has an invalid input schema.",
                outcome=ToolOutcome.VALIDATION_ERROR,
                error_category="invalid_schema",
                error_code="INVALID_TOOL_SCHEMA",
            )

        try:
            inspect.signature(self.tool.execute).bind(**arguments)
        except (TypeError, ValueError) as exc:
            return self._validation_failure(call_id, f"signature validation failed: {exc}")

        try:
            raw, timed_out = self._execute(arguments, context)
        except ToolError as exc:
            return ToolResult.failure(
                call_id,
                self.spec.name,
                exc.safe_message,
                outcome=exc.outcome,
                error_category=exc.category,
                error_code=exc.code,
                retryable=exc.retryable,
                evidence=exc.evidence,
            )
        except Exception as exc:  # noqa: BLE001 - the adapter is the trust boundary
            return ToolResult.failure(
                call_id,
                self.spec.name,
                f"Error: {self.spec.name} failed unexpectedly. Details were recorded in trace.",
                outcome=ToolOutcome.UNKNOWN_ERROR,
                error_category="tool_exception",
                error_code=f"UNEXPECTED_{type(exc).__name__.upper()}",
            )

        if timed_out:
            return ToolResult.failure(
                call_id,
                self.spec.name,
                f"Error: {self.spec.name} timed out.",
                outcome=ToolOutcome.TIMEOUT,
                error_category="timeout",
                error_code="TOOL_TIMEOUT",
                retryable=True,
            )
        return self._normalise_result(call_id, raw)

    def _execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> tuple[Any, bool]:
        if (
            context is not None
            and getattr(self.tool, "context_aware", False)
            and callable(getattr(self.tool, "execute_with_context", None))
        ):
            # Context-aware tools own their deadline and process lifetime.  The
            # daemon-thread fallback below could only abandon a call, not stop
            # the work it had already started.
            return self.tool.execute_with_context(arguments, context), False

        timeout = self.spec.timeout_seconds
        if timeout is None or getattr(self.tool, "manages_own_timeout", False):
            return self.tool.execute(**arguments), False

        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def target() -> None:
            try:
                result_queue.put((True, self.tool.execute(**arguments)))
            except BaseException as exc:  # noqa: BLE001 - propagated on caller thread
                result_queue.put((False, exc))

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            return None, True
        succeeded, payload = result_queue.get_nowait()
        if succeeded:
            return payload, False
        raise payload

    def _normalise_result(self, call_id: str, raw: Any) -> ToolResult:
        if isinstance(raw, ToolResult):
            result = replace(raw, call_id=call_id, tool_name=self.spec.name)
        elif isinstance(raw, str) and _looks_like_legacy_error(raw):
            result = ToolResult.failure(
                call_id,
                self.spec.name,
                raw,
                outcome=ToolOutcome.BUSINESS_ERROR,
                error_category="tool_error",
                error_code="LEGACY_TOOL_ERROR",
            )
        else:
            result = ToolResult.success(
                call_id,
                self.spec.name,
                raw,
                evidence=(f"tool:{call_id}:{self.spec.name}",),
            )

        if result.ok:
            try:
                _validate_schema(result.data, self.spec.output_schema)
                json.dumps(result.data, ensure_ascii=False)
            except (_SchemaValidationError, TypeError, ValueError):
                return ToolResult.failure(
                    call_id,
                    self.spec.name,
                    f"Error: {self.spec.name} returned an invalid result.",
                    outcome=ToolOutcome.UNKNOWN_ERROR,
                    error_category="output_validation",
                    error_code="INVALID_TOOL_OUTPUT",
                )
        return result

    def _validation_failure(self, call_id: str, detail: str) -> ToolResult:
        return ToolResult.failure(
            call_id,
            self.spec.name,
            f"Error: bad arguments for {self.spec.name}: {detail}",
            outcome=ToolOutcome.VALIDATION_ERROR,
            error_category="invalid_arguments",
            error_code="INVALID_TOOL_ARGUMENTS",
        )


def _looks_like_legacy_error(value: str) -> bool:
    return value.startswith(("Error:", "Invalid regex:", "Sub-agent error:", "⚠ Blocked:"))
