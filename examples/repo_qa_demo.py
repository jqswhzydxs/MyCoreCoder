"""Real retrieval and checked citations with an explicitly extractive fixture.

Run: python -m examples.repo_qa_demo
No model credentials/network/charges. Does not measure live answer quality.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.answering import FixtureAnswerBackend, RepositoryAnswerer
from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import FixtureEmbeddingProvider
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.workflows.budget_ledger import BudgetLedger, WorkflowBudget


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="corecoder-qa-") as directory:
        root = Path(directory)
        (root / "corecoder").mkdir()
        source = root / "corecoder/runtime.py"
        source.write_text('class RunStore:\n    """Journal execution facts before recovery."""\n', encoding="utf-8")
        ledger = BudgetLedger(WorkflowBudget(max_requests=30, max_tokens=1_000_000))
        embeddings = EmbeddingGateway(FixtureEmbeddingProvider(16), ledger)
        with KnowledgeService(create_scope(root), embeddings) as service:
            first = service.index()
            answerer = RepositoryAnswerer(service, ModelGateway(FixtureAnswerBackend(), ledger))
            normal = answerer.ask("RunStore")
            missing = answerer.ask("ImaginaryQuantumPlanner")
            assert normal.status == "answered" and normal.sources
            assert missing.status == "insufficient"
            source.write_text(source.read_text(encoding="utf-8") + "\n# Updated source generation\n", encoding="utf-8")
            refreshed = answerer.ask("RunStore")
            assert refreshed.status == "answered" and refreshed.snapshot_id != first.snapshot_id
            assert all(item["snapshot_id"] == refreshed.snapshot_id for item in refreshed.sources)
            return {"mode": "offline_extractive_fixture", "answer": normal.to_dict(), "missing": missing.to_dict(),
                    "after_edit": refreshed.to_dict()}


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
