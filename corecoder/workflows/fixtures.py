"""Explicit offline pipeline backend. It makes no semantic quality claims."""

from __future__ import annotations

import json

from ..knowledge.answering import FixtureAnswerBackend
from ..llm import LLMResponse


class FixtureWorkflowBackend(FixtureAnswerBackend):
    model = "workflow-extractive-fixture-v1"
    is_fixture = True

    def __init__(self, intent="ask"):
        self.intent = intent

    def chat_once(self, messages, **kwargs):
        try:
            payload = json.loads(messages[-1]["content"])
        except (ValueError, TypeError):
            return LLMResponse(content="Offline workflow fixture completed without implementing code edits.")
        properties = payload["output_schema"]["properties"]
        data = payload["input_data"]
        if "task_type" in properties:
            refs = [item["id"] for item in data["evidence"][:1]]
            return LLMResponse(content=json.dumps({"task_type": "code_change" if self.intent == "code" else "repo_qa",
                "goal": data["goal"][:2000], "evidence_ids": refs,
                "steps": [{"id": "s1", "action": "Handle the explicit request using current evidence", "expected_evidence": refs}],
                "unknowns": [], "requires_code_change": self.intent == "code"}))
        if "candidates" in properties:
            return LLMResponse(content='{"candidates": []}')
        return super().chat_once(messages, **kwargs)
