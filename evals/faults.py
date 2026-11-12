"""Deterministic fault injection used only by the offline eval harness."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from corecoder.llm import LLMResponse, ToolCall
from corecoder.run_store import InjectedProcessCrash
from corecoder.tools.protocol import ToolError, ToolOutcome

TRANSIENT_LLM_FAULTS = frozenset({"429", "5xx", "connection_interrupted"})


class InjectedLLMFault(RuntimeError):
    """Offline stand-in for a provider transient; never contacts a provider."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(f"deterministic injected LLM fault: {category}")


class InjectedResponseLoss(InjectedProcessCrash):
    """The side effect completed but the caller lost its response."""


@dataclass(slots=True)
class FaultTelemetry:
    """Stable accounting. Latency is simulated so reports are reproducible."""

    model_attempts: int = 0
    model_responses: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    injected_faults: list[str] = field(default_factory=list)
    tool_executions: list[dict[str, Any]] = field(default_factory=list)
    crash_events: list[str] = field(default_factory=list)

    @property
    def estimated_cost_usd(self) -> float:
        # A stable harness rate, not a provider invoice. Reports state this limit.
        return self.prompt_tokens / 1_000_000 + self.completion_tokens * 2 / 1_000_000


class FaultPlan:
    """One-shot deterministic faults declared in case JSON."""

    def __init__(self, config: dict[str, Any] | None, telemetry: FaultTelemetry):
        self.config = dict(config or {})
        self.telemetry = telemetry
        self._fired: set[str] = set()
        self._tool_attempts: Counter[str] = Counter()

    def llm_fault(self, response_index: int, attempt: int) -> str | None:
        item = self.config.get("llm") or {}
        category = item.get("type")
        if category not in TRANSIENT_LLM_FAULTS:
            return None
        if int(item.get("response_index", 0)) != response_index:
            return None
        count = int(item.get("times", 1))
        return category if attempt <= count else None

    def tool_fault(self, tool_name: str) -> str | None:
        item = self.config.get("tool") or {}
        if item.get("name") != tool_name:
            return None
        self._tool_attempts[tool_name] += 1
        if self._tool_attempts[tool_name] > int(item.get("times", 1)):
            return None
        return str(item.get("type") or "") or None

    def crash_injector(self, event: str, _context: dict[str, Any]) -> None:
        item = self.config.get("process") or {}
        configured = item.get("event")
        key = f"process:{event}"
        if configured == event and key not in self._fired:
            self._fired.add(key)
            self.telemetry.injected_faults.append(key)
            self.telemetry.crash_events.append(event)
            raise InjectedProcessCrash(f"deterministic crash at {event}")

    def mark_response_loss(self, tool_name: str) -> bool:
        item = self.config.get("tool") or {}
        key = f"response_lost:{tool_name}"
        if (
            item.get("name") == tool_name
            and item.get("type") == "response_lost"
            and key not in self._fired
        ):
            self._fired.add(key)
            self.telemetry.injected_faults.append(key)
            self.telemetry.crash_events.append("response_lost")
            return True
        return False

    def mutate_response(self, response_index: int, response: LLMResponse) -> None:
        """Inject a malformed raw argument payload at the model boundary."""
        item = self.config.get("llm") or {}
        key = f"invalid_json_arguments:{response_index}"
        if (
            item.get("type") == "invalid_json_arguments"
            and int(item.get("response_index", 0)) == response_index
            and key not in self._fired
        ):
            call_index = int(item.get("call_index", 0))
            if 0 <= call_index < len(response.tool_calls):
                response.tool_calls[call_index].arguments = item.get("raw", "{")
                self._fired.add(key)
                self.telemetry.injected_faults.append(key)


class FaultInjectingLLM:
    """Scripted LLM with bounded retry and deterministic provider faults."""

    model = "scripted-eval-v2"

    def __init__(
        self,
        responses: list[LLMResponse],
        plan: FaultPlan,
        telemetry: FaultTelemetry,
        *,
        max_retries: int = 3,
    ):
        self._responses = list(responses)
        self.plan = plan
        self.telemetry = telemetry
        self.max_retries = max_retries
        self.calls: list[ToolCall] = []
        self.rounds = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @property
    def estimated_cost(self) -> float:
        return self.telemetry.estimated_cost_usd

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None) -> LLMResponse:
        del messages, tools, on_reasoning
        response_index = self.rounds
        self.rounds += 1
        for attempt in range(1, self.max_retries + 1):
            self.telemetry.model_attempts += 1
            self.telemetry.latency_ms += 7.0
            category = self.plan.llm_fault(response_index, attempt)
            if category is not None:
                self.telemetry.injected_faults.append(f"llm:{category}")
                if attempt == self.max_retries:
                    raise InjectedLLMFault(category)
                continue
            break
        if not self._responses:
            raise RuntimeError("FaultInjectingLLM ran out of scripted responses")
        response = self._responses.pop(0)
        self.plan.mutate_response(response_index, response)
        prompt_tokens = response.prompt_tokens or 12
        completion_tokens = response.completion_tokens or max(1, len(response.content.split()))
        response.prompt_tokens = prompt_tokens
        response.completion_tokens = completion_tokens
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.telemetry.prompt_tokens += prompt_tokens
        self.telemetry.completion_tokens += completion_tokens
        self.telemetry.model_responses += 1
        self.telemetry.latency_ms += 3.0
        self.calls.extend(response.tool_calls)
        if on_token and response.content:
            on_token(response.content)
        return response


class FaultInjectingTool:
    """Transparent tool proxy that can fail before or after the side effect."""

    def __init__(self, tool, plan: FaultPlan, telemetry: FaultTelemetry):
        self.tool = tool
        self.plan = plan
        self.telemetry = telemetry

    def __getattr__(self, name: str):
        return getattr(self.tool, name)

    @property
    def spec(self):
        return self.tool.spec

    def schema(self):
        return self.tool.schema()

    def resource_keys(self, arguments):
        return self.tool.resource_keys(arguments)

    def data_dependencies(self, arguments):
        return self.tool.data_dependencies(arguments)

    def execute(self, **arguments):
        fault = self.plan.tool_fault(self.name)
        if fault == "timeout":
            self.telemetry.injected_faults.append(f"tool:{self.name}:timeout")
            self.telemetry.latency_ms += float(
                (self.plan.config.get("tool") or {}).get("latency_ms", 1000)
            )
            raise ToolError(
                f"Error: {self.name} timed out.",
                category="timeout",
                code="INJECTED_TOOL_TIMEOUT",
                retryable=True,
                outcome=ToolOutcome.TIMEOUT,
            )
        if fault == "transient_failure":
            self.telemetry.injected_faults.append(f"tool:{self.name}:transient_failure")
            self.telemetry.latency_ms += 5.0
            raise ToolError(
                f"Error: {self.name} had a transient failure.",
                category="transient_failure",
                code="INJECTED_TRANSIENT_FAILURE",
                retryable=True,
            )

        result = self.tool.execute(**arguments)
        self.telemetry.latency_ms += 5.0
        self.telemetry.tool_executions.append(
            {
                "tool_name": self.name,
                "arguments": json.loads(
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
                ),
                "side_effect": self.spec.side_effect,
            }
        )
        if self.plan.mark_response_loss(self.name):
            raise InjectedResponseLoss(
                f"{self.name} completed but its response was deterministically lost"
            )
        return result
