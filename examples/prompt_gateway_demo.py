"""Offline demonstration of the optional Prompt chain and shared ledger.

Run: python -m examples.prompt_gateway_demo
No credentials are loaded and no network requests are made.
"""

from __future__ import annotations

import json

from corecoder.integrations.langchain_bridge import StructuredPromptChain
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.llm import LLMResponse
from corecoder.workflows.budget_ledger import BudgetLedger, WorkflowBudget


class DemoBackend:
    """A fixed fixture proves plumbing, not model quality."""

    model = "scripted-prompt-demo"

    def chat_once(self, messages, *, tools, timeout_seconds, max_tokens):
        payload = json.loads(messages[-1]["content"])
        evidence = payload["input_data"]["evidence"]
        return LLMResponse(
            content=json.dumps({
                "status": "answered" if evidence else "insufficient",
                "answer": "Completion is established by verifier evidence." if evidence else "",
                "evidence_ids": [evidence[0]["id"]] if evidence else [],
                "unknowns": [] if evidence else ["No repository evidence was supplied."],
            }),
        )


def run_demo() -> dict:
    ledger = BudgetLedger(WorkflowBudget(max_requests=3, max_tokens=30_000))
    chain = StructuredPromptChain(ModelGateway(DemoBackend(), ledger), "repo_answer.v2")
    result = chain.invoke({
        "question": "How is completion established?",
        "evidence": [{"id": "fixture-verification", "text": "Only a trusted Verifier establishes completion."}],
        "memories": [],
    })
    no_evidence = chain.invoke({"question": "How does an imaginary module work?", "evidence": [], "memories": []})
    return {
        "mode": "deterministic_offline_fixture",
        "template_id": result.template_id,
        "template_sha256": result.template_sha256,
        "answer": result.value.model_dump(),
        "without_evidence": no_evidence.value.model_dump(),
        "budget": ledger.snapshot(),
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
