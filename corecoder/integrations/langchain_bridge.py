"""Optional LangChain Prompt -> gateway Runnable -> validated proposal chain."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..prompts.registry import load_prompt
from .model_gateway import ModelGateway, validate_messages


def _dependencies():
    try:
        from langchain_core.messages import SystemMessage, convert_to_openai_messages
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.runnables import RunnableLambda
    except ImportError:
        raise ImportError('Prompt chains require: python -m pip install "corecoder[knowledge]"') from None
    return SystemMessage, convert_to_openai_messages, ChatPromptTemplate, RunnableLambda


def to_messages(value) -> list[dict]:
    """Preserve assistant/tool call pairing and OpenAI structured arguments."""
    _, convert, _, _ = _dependencies()
    if hasattr(value, "to_messages"):
        value = value.to_messages()
    messages = convert(value, text_format="string")
    if isinstance(messages, dict):
        messages = [messages]
    validate_messages(messages)
    return messages


def gateway_runnable(gateway: ModelGateway, *, operation: str = "answer"):
    _, _, _, runnable = _dependencies()
    return runnable(lambda value: gateway.invoke(to_messages(value), operation=operation))


class StructuredOutputError(ValueError):
    """A proposal still violates its contract after the single allowed repair."""


@dataclass(frozen=True)
class PromptResult:
    value: object
    template_id: str
    template_sha256: str
    repaired: bool


class StructuredPromptChain:
    """Actual LangChain composition; repairs share the caller's model ledger.

    This chain never executes tools or commits memory. LangChain callbacks are
    not accepted because they may persist prompt content outside local storage.
    """

    def __init__(self, gateway: ModelGateway, template_id: str = "repo_answer.v1"):
        system_message, _, prompt_type, runnable = _dependencies()
        from ..prompts.schemas import OUTPUT_SCHEMAS

        self.gateway = gateway
        self.template = load_prompt(template_id)
        self.kind = template_id.split(".")[0]
        self.schema = OUTPUT_SCHEMAS[self.kind]
        self._prompt = prompt_type.from_messages([
            system_message(content=self.template.text),
            ("human", "{payload}"),
        ])
        self._runnable = runnable

    def invoke(self, values: dict) -> PromptResult:
        from pydantic import ValidationError

        evidence = values.get("evidence")
        if not isinstance(evidence, list) or any(not isinstance(e, dict) or not isinstance(e.get("id"), str) for e in evidence):
            raise ValueError("evidence must be a list of objects with string ids")
        known_ids = {item["id"] for item in evidence}
        if len(known_ids) != len(evidence):
            raise ValueError("evidence ids must be unique")
        errors: list[dict] = []
        for repair in range(2):
            payload = self.template.payload(values, schema=self.schema.model_json_schema(), repair_errors=errors)
            operation = "schema_repair" if repair else {
                "repo_answer": "answer", "task_plan": "plan", "memory_extract": "memory_extract", "query_rewrite": "query_rewrite",
            }[self.kind]

            def parse(result):
                if result.response.tool_calls:
                    raise StructuredOutputError("structured proposals cannot contain tool calls")
                return self.schema.model_validate_json(result.response.content)

            chain = self._prompt | gateway_runnable(self.gateway, operation=operation) | self._runnable(parse)
            try:
                # Passing an explicit config avoids reading tracing environment
                # configuration; process-global tracing must also be disabled.
                with _local_tracing():
                    proposal = chain.invoke({"payload": payload}, config={"callbacks": []})
            except ValidationError as exc:
                # Field locations/types only: never echo invalid values, provider
                # messages, custom validation context, or raw model text.
                errors = [{"type": error["type"]} for error in exc.errors(include_input=False, include_context=False)]
                continue
            if not proposal.evidence_ids_used().issubset(known_ids):
                errors = [{"type": "unknown_evidence_id"}]
                continue
            if self.kind == "memory_extract" and any(c.source_ref != values["source_ref"] for c in proposal.candidates):
                errors = [{"type": "source_reference_mismatch"}]
                continue
            return PromptResult(proposal, self.template.template_id, self.template.sha256, bool(repair))
        raise StructuredOutputError("model output failed validation after one repair: " + json.dumps(errors)) from None


def _local_tracing():
    from langsmith import tracing_context

    return tracing_context(enabled=False)
