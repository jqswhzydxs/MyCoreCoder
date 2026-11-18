"""Budgeted model calls with one retry owner and content-free failure metadata."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Protocol

from openai import APIConnectionError, APITimeoutError

from ..llm import LLMProtocolError, LLMResponse
from ..workflows.budget_ledger import BudgetExceeded, BudgetLedger, TokenRates


class ErrorCategory(str, Enum):
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER = "server"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


class GatewayError(RuntimeError):
    def __init__(self, category: ErrorCategory, *, retryable: bool = False, status_code: int | None = None):
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(f"model request failed: {category.value}")


class ChatBackend(Protocol):
    model: str

    def chat_once(self, messages: list[dict], *, tools: list[dict] | None, timeout_seconds: float, max_tokens: int) -> LLMResponse:
        """Perform exactly one API attempt; do not retry internally."""
        ...


@dataclass(frozen=True)
class CallRecord:
    operation: str
    attempt: int
    duration_ms: float
    category: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class ModelResult:
    response: LLMResponse
    model: str
    request_id: str | None


def validate_messages(messages: list[dict]) -> None:
    """Reject orphaned/duplicate replies and incomplete assistant/tool batches."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty list")
    pending: set[str] = set()
    seen_calls: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise TypeError("each message must be an object")
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("unsupported message role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError("tool reply has no pending assistant call")
            pending.remove(call_id)
            continue
        if pending:
            raise ValueError("assistant tool batch is missing replies")
        calls = message.get("tool_calls") or []
        if calls and role != "assistant":
            raise ValueError("only assistant messages may request tools")
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
                raise ValueError("tool calls require an id")
            if call["id"] in seen_calls:
                raise ValueError("tool call ids must be unique")
            function = call.get("function", {})
            if call.get("type") != "function" or not isinstance(function, dict) or not function.get("name"):
                raise ValueError("tool calls require a function name")
            try:
                arguments = json.loads(function["arguments"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("tool arguments must be a JSON object") from None
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be a JSON object")
            pending.add(call["id"])
            seen_calls.add(call["id"])
    if pending:
        raise ValueError("assistant tool batch is missing replies")


def classify_error(exc: Exception) -> GatewayError:
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = None
    if status in {401, 403}:
        return GatewayError(ErrorCategory.AUTHENTICATION, status_code=status)
    if status == 429:
        return GatewayError(ErrorCategory.RATE_LIMIT, retryable=True, status_code=status)
    if status in {408, 504} or isinstance(exc, (APITimeoutError, TimeoutError)):
        return GatewayError(ErrorCategory.TIMEOUT, retryable=True, status_code=status)
    if status is not None and status >= 500:
        return GatewayError(ErrorCategory.SERVER, retryable=True, status_code=status)
    if status is not None and 400 <= status < 500:
        return GatewayError(ErrorCategory.INVALID_REQUEST, status_code=status)
    if isinstance(exc, (APIConnectionError, ConnectionError)):
        return GatewayError(ErrorCategory.CONNECTION, retryable=True)
    if isinstance(exc, LLMProtocolError):
        return GatewayError(ErrorCategory.INVALID_RESPONSE)
    return GatewayError(ErrorCategory.UNKNOWN)


class ModelGateway:
    """A gateway owns retries; the injected backend must expose ``chat_once``.

    No prompts, provider exception bodies, keys, or tool output enter records.
    A gateway and its shared ledger are intended for a single workflow.
    """

    def __init__(
        self,
        backend: ChatBackend,
        ledger: BudgetLedger | None = None,
        *,
        max_attempts: int = 3,
        request_timeout: float = 30.0,
        max_output_tokens: int = 1024,
        rates: TokenRates | None = None,
        sleep=time.sleep,
    ):
        if not callable(getattr(backend, "chat_once", None)):
            raise TypeError("backend must implement the single-attempt chat_once contract")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be an integer between 1 and 5")
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request_timeout must be finite and positive")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        self.backend = backend
        self.ledger = ledger or BudgetLedger()
        self.max_attempts = max_attempts
        self.request_timeout = request_timeout
        self.max_output_tokens = max_output_tokens
        self.rates = rates
        self._sleep = sleep
        self.records: list[CallRecord] = []

    def invoke(self, messages: list[dict], *, operation: str = "answer", tools: list[dict] | None = None) -> ModelResult:
        if operation not in {"answer", "plan", "memory_extract", "schema_repair", "summarize", "query_rewrite", "agent"}:
            raise ValueError("unsupported model operation")
        validate_messages(messages)
        # UTF-8 bytes deliberately overestimate ordinary text token counts.
        prompt_estimate = len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode("utf-8"))
        for attempt in range(1, self.max_attempts + 1):
            reservation = self.ledger.reserve(prompt_estimate, self.max_output_tokens, self.rates)
            started = time.monotonic()
            try:
                response = self.backend.chat_once(
                    messages,
                    tools=tools,
                    timeout_seconds=min(self.request_timeout, self.ledger.remaining_seconds()),
                    max_tokens=self.max_output_tokens,
                )
                if not isinstance(response, LLMResponse):
                    raise LLMProtocolError("backend did not return an LLMResponse")
            except Exception as exc:  # noqa: BLE001 - normalize provider failures without leaking response bodies
                self.ledger.settle(reservation, rates=self.rates)
                failure = classify_error(exc)
                self.records.append(CallRecord(operation, attempt, (time.monotonic() - started) * 1000, failure.category.value))
                adapter = getattr(self.backend, "adapt_gateway_request", None)
                adapted = (
                    failure.category is ErrorCategory.INVALID_REQUEST
                    and attempt < self.max_attempts and callable(adapter) and adapter(exc)
                )
                if (not failure.retryable and not adapted) or attempt == self.max_attempts:
                    raise failure from None
                delay = 0.0 if adapted else self._retry_delay(exc, attempt)
                if delay >= self.ledger.remaining_seconds():
                    raise BudgetExceeded("workflow deadline prevents retry") from None
                self._sleep(delay)
                continue
            self.ledger.settle(
                reservation,
                prompt_tokens=response.prompt_tokens if response.usage_available else None,
                completion_tokens=response.completion_tokens if response.usage_available else None,
                rates=self.rates,
            )
            self.records.append(CallRecord(
                operation, attempt, (time.monotonic() - started) * 1000, None,
                response.prompt_tokens if response.usage_available else None,
                response.completion_tokens if response.usage_available else None,
            ))
            self.ledger.check()
            return ModelResult(response, self.backend.model, response.request_id)
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        headers = getattr(getattr(exc, "response", None), "headers", {})
        try:
            delay = float(headers.get("retry-after", 2 ** (attempt - 1)))
        except (TypeError, ValueError, AttributeError):
            delay = float(2 ** (attempt - 1))
        return max(0.0, min(delay, 30.0)) if math.isfinite(delay) else float(2 ** (attempt - 1))

    def metrics(self) -> dict:
        return {"budget": self.ledger.snapshot(), "calls": [asdict(record) for record in self.records]}
