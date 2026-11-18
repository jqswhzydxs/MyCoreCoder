"""Compatible buffered Agent model calls charged to the workflow gateway."""

from __future__ import annotations

from ..budget import CancellationToken, RunStopped
from ..state import ExitReason
from ..workflows.budget_ledger import BudgetExceeded
from .model_gateway import ModelGateway


class AgentLLMBridge:
    def __init__(self, gateway: ModelGateway, *, cancellation_token: CancellationToken | None = None):
        self.gateway = gateway
        self.cancellation_token = cancellation_token or CancellationToken()
        self.model = gateway.backend.model
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._unknown_usage = False
        self._cost = 0.0
        self._cost_known = gateway.rates is not None

    @property
    def estimated_cost(self):
        return self._cost if self._cost_known else None

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None, on_stream_activity=None):
        return self._chat(messages, tools, on_token, on_stream_activity, operation="agent")

    def summarize(self, messages, **kwargs):
        return self._chat(messages, kwargs.get("tools"), kwargs.get("on_token"), None, operation="summarize")

    def _chat(self, messages, tools, on_token, on_stream_activity, *, operation):
        if self.cancellation_token.is_cancelled:
            raise RunStopped(ExitReason.CANCELLED, "Workflow was cancelled.", cancelled=True)
        before = self.gateway.ledger.snapshot()
        try:
            response = self.gateway.invoke(messages, operation=operation, tools=tools).response
        except BudgetExceeded:
            if self.cancellation_token.is_cancelled:
                raise RunStopped(ExitReason.CANCELLED, "Workflow was cancelled.", cancelled=True) from None
            raise RunStopped(ExitReason.WORKFLOW_BUDGET, "Shared workflow model budget is exhausted.") from None
        if response.usage_available:
            self.total_prompt_tokens += response.prompt_tokens
            self.total_completion_tokens += response.completion_tokens
        else:
            # Agent's RunBudget must also see conservative consumption. The
            # workflow ledger remains the only authoritative global total.
            delta = self.gateway.ledger.snapshot()["tokens"] - before["tokens"]
            self.total_prompt_tokens += delta
            self._unknown_usage = True
        self._cost_known = self._cost_known and response.usage_available
        if self.gateway.rates and response.usage_available:
            self._cost += self.gateway.rates.cost(response.prompt_tokens, response.completion_tokens)
        if self.cancellation_token.is_cancelled:
            raise RunStopped(ExitReason.CANCELLED, "Workflow was cancelled.", cancelled=True)
        # chat_once buffers the response. These callbacks occur after the
        # request completes; the adapter does not promise token streaming.
        if on_token and response.content:
            on_token(response.content)
        if on_stream_activity:
            for index, _ in enumerate(response.tool_calls):
                on_stream_activity({"activity": "tool_call_detected", "tool_index": index})
        return response
