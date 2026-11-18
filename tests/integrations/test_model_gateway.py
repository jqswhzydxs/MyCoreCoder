"""Offline failures, retry ownership, accounting and message safety."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from corecoder.config import Config, EmbeddingConfig
from corecoder.integrations.model_gateway import ErrorCategory, GatewayError, ModelGateway, validate_messages
from corecoder.llm import LLM, LiteLLM, LLMProtocolError, LLMResponse
from corecoder.workflows.budget_ledger import BudgetExceeded, BudgetLedger, TokenRates, WorkflowBudget


class Backend:
    model = "offline-fixture"

    def __init__(self, *outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def chat_once(self, messages, **kwargs):
        self.calls.append(kwargs)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


class ProviderFailure(Exception):
    def __init__(self, status):
        self.status_code = status
        self.response = SimpleNamespace(headers={"retry-after": "0"})
        super().__init__("SECRET_PROVIDER_RESPONSE")


MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize(("status", "category", "attempts"), [
    (400, ErrorCategory.INVALID_REQUEST, 1),
    (401, ErrorCategory.AUTHENTICATION, 1),
    (403, ErrorCategory.AUTHENTICATION, 1),
    (429, ErrorCategory.RATE_LIMIT, 3),
    (503, ErrorCategory.SERVER, 3),
])
def test_error_categories_and_retry_limit_do_not_leak_provider_text(status, category, attempts):
    backend = Backend(*(ProviderFailure(status) for _ in range(3)))
    gateway = ModelGateway(backend, sleep=lambda _: None)
    with pytest.raises(GatewayError) as error:
        gateway.invoke(MESSAGES)
    assert error.value.category is category
    assert len(backend.calls) == attempts
    assert "SECRET_PROVIDER_RESPONSE" not in str(error.value)
    assert "SECRET_PROVIDER_RESPONSE" not in json.dumps(gateway.metrics())
    assert gateway.ledger.snapshot()["unknown_usage_requests"] == attempts


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError()])
def test_transient_retry_shares_ledger_and_output_limit(failure):
    backend = Backend(failure, LLMResponse(content="ok", prompt_tokens=10, completion_tokens=2, usage_available=True))
    ledger = BudgetLedger(WorkflowBudget(max_requests=2))
    result = ModelGateway(backend, ledger, max_output_tokens=20, sleep=lambda _: None).invoke(MESSAGES)
    assert result.response.content == "ok"
    assert len(backend.calls) == ledger.snapshot()["requests"] == 2
    assert backend.calls[0]["max_tokens"] == 20


def test_missing_usage_and_rates_remain_unknown_and_charge_reserved_tokens():
    gateway = ModelGateway(Backend(LLMResponse(content="ok")))
    gateway.invoke(MESSAGES)
    usage = gateway.ledger.snapshot()
    assert usage["tokens"] > 1024
    assert usage["unknown_usage_requests"] == 1
    assert usage["cost_usd"] is None and usage["cost_unavailable"]


def test_cost_limit_requires_rates_before_request():
    backend = Backend()
    ledger = BudgetLedger(WorkflowBudget(max_cost_usd=1))
    with pytest.raises(BudgetExceeded, match="trusted token rates"):
        ModelGateway(backend, ledger).invoke(MESSAGES)
    assert backend.calls == []


def test_cost_and_token_reservations_prevent_parallel_overspending():
    ledger = BudgetLedger(WorkflowBudget(max_tokens=100, max_cost_usd=0.0001))
    rates = TokenRates(1, 1)
    reserved = ledger.reserve(50, 40, rates)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(20, 0, rates)
    ledger.settle(reserved, prompt_tokens=40, completion_tokens=20, rates=rates)
    assert ledger.snapshot()["tokens"] == 60
    assert ledger.snapshot()["cost_usd"] == pytest.approx(0.00006)
    with pytest.raises(ValueError, match="already been settled"):
        ledger.settle(reserved)


def test_budget_exhaustion_never_starts_an_extra_retry():
    backend = Backend(ProviderFailure(429), LLMResponse(content="unused"))
    ledger = BudgetLedger(WorkflowBudget(max_requests=1))
    with pytest.raises(BudgetExceeded):
        ModelGateway(backend, ledger, sleep=lambda _: None).invoke(MESSAGES)
    assert len(backend.calls) == 1


def test_provider_usage_overrun_is_recorded_and_stops_before_returning_success():
    backend = Backend(LLMResponse(content="over-budget", prompt_tokens=500, completion_tokens=600, usage_available=True))
    ledger = BudgetLedger(WorkflowBudget(max_tokens=1000))
    with pytest.raises(BudgetExceeded, match="reported usage"):
        ModelGateway(backend, ledger, max_output_tokens=50).invoke(MESSAGES)
    assert ledger.snapshot()["tokens"] == 1100
    assert len(backend.calls) == 1


def test_foreign_ledger_reservation_cannot_settle_another_request():
    first, second = BudgetLedger(), BudgetLedger()
    a = first.reserve(10, 10)
    b = second.reserve(10, 10)
    with pytest.raises(ValueError):
        second.settle(a)
    second.settle(b)
    first.settle(a)


def test_deadline_stops_before_request_and_before_backoff():
    now = [0.0]
    ledger = BudgetLedger(WorkflowBudget(deadline_seconds=0.5), clock=lambda: now[0])
    backend = Backend(ConnectionError())
    with pytest.raises(BudgetExceeded, match="prevents retry"):
        ModelGateway(backend, ledger).invoke(MESSAGES)
    assert len(backend.calls) == 1
    now[0] = 1.0
    with pytest.raises(BudgetExceeded):
        ModelGateway(backend, ledger).invoke(MESSAGES)
    assert len(backend.calls) == 1


def test_incomplete_tool_batches_are_rejected_before_network():
    call = {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}
    assistant = {"role": "assistant", "content": None, "tool_calls": [call]}
    reply = {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    validate_messages([*MESSAGES, assistant, reply])
    for messages in ([reply], [assistant], [assistant, *MESSAGES], [assistant, reply, reply], [assistant, reply, assistant]):
        backend = Backend()
        with pytest.raises(ValueError):
            ModelGateway(backend).invoke(messages)
        assert backend.calls == []


def chunk(*, text=None, calls=None, finish=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=calls), finish_reason=finish)], usage=usage,
    )


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.response = SimpleNamespace(headers={"x-request-id": "fixture-request"})

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def test_single_attempt_disables_sdk_retries_closes_stream_and_keeps_request_id():
    llm = LLM(model="fixture", api_key="unused")
    llm.client = Mock()
    stream = Stream([chunk(text="ok", finish="stop", usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))])
    llm.client.with_options.return_value.chat.completions.create.return_value = stream
    result = llm.chat_once(MESSAGES, max_tokens=15, timeout_seconds=2)
    assert stream.closed
    assert result.request_id == "fixture-request" and result.usage_available
    llm.client.with_options.assert_called_once_with(max_retries=0, timeout=2)
    assert llm.client.with_options.return_value.chat.completions.create.call_args.kwargs["max_tokens"] == 15


def test_recognized_parameter_adaptation_counts_as_another_gateway_request():
    from tests.test_core import TestLLMParamFallback

    llm = LLM(model="fixture", api_key="unused")
    llm.client = Mock()
    create = llm.client.with_options.return_value.chat.completions.create
    create.side_effect = [
        TestLLMParamFallback._bad_request("Unsupported parameter: 'max_tokens'"),
        Stream([chunk(text="ok", finish="stop")]),
    ]
    gateway = ModelGateway(llm)
    assert gateway.invoke(MESSAGES).response.content == "ok"
    assert gateway.ledger.snapshot()["requests"] == 2
    assert create.call_args.kwargs["max_completion_tokens"] == 1024
    assert "max_tokens" not in create.call_args.kwargs


def test_stream_drop_discards_partial_tool_calls_and_always_closes():
    llm = LLM(model="fixture", api_key="unused")
    partial = SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name="write", arguments='{"path":'))
    stream = Stream([chunk(calls=[partial])])
    llm._create_once = Mock(return_value=stream)
    with pytest.raises(LLMProtocolError):
        llm.chat_once(MESSAGES)
    assert stream.closed


def test_litellm_single_attempt_disables_both_retry_layers(monkeypatch):
    import sys

    fake = SimpleNamespace(completion=Mock(return_value=Stream([chunk(text="ok", finish="stop")])))
    monkeypatch.setitem(sys.modules, "litellm", fake)
    response = LiteLLM("fixture", api_key="unused").chat_once(MESSAGES)
    assert response.content == "ok"
    assert fake.completion.call_args.kwargs["num_retries"] == 0
    assert fake.completion.call_args.kwargs["max_retries"] == 0


def test_embedding_configuration_is_independent_and_chat_key_is_not_in_repr(monkeypatch):
    monkeypatch.setenv("CORECODER_MODEL", "chat-only")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://chat.invalid")
    monkeypatch.delenv("CORECODER_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("CORECODER_EMBEDDING_BASE_URL", raising=False)
    assert EmbeddingConfig.from_env().model == ""
    assert EmbeddingConfig.from_env().base_url is None
    assert "SECRET_TEST_KEY" not in repr(Config(api_key="SECRET_TEST_KEY"))
