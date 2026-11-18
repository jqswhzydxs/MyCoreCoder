"""Offline Chroma/FTS5 generations with real assertions in a temporary project.

Run from the Python project root: python -m examples.knowledge_index_demo
Requires corecoder[knowledge]. No network, model weights, or credentials.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import FixtureEmbeddingProvider
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.workflows.budget_ledger import BudgetLedger, WorkflowBudget


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="corecoder-knowledge-") as directory:
        root = Path(directory)
        (root / "corecoder").mkdir()
        (root / "docs").mkdir()
        source = root / "corecoder/journal.py"
        source.write_text("class RunStore:\n    def recover(self):\n        return 'reconciled'\n", encoding="utf-8")
        (root / "docs/design.md").write_text("# 验证与恢复\nRunStore 保存执行事实，避免崩溃后重复工具副作用。\n", encoding="utf-8")
        ledger = BudgetLedger(WorkflowBudget(max_requests=50, max_tokens=1_000_000))
        gateway = EmbeddingGateway(FixtureEmbeddingProvider(16), ledger)
        with KnowledgeService(create_scope(root), gateway) as service:
            cold = service.index()
            warm = service.index()
            assert warm.embedding_requests == 0 and warm.snapshot_id == cold.snapshot_id
            source.write_text(source.read_text(encoding="utf-8") + "\ndef reconcile():\n    return True\n", encoding="utf-8")
            modified = service.index()
            assert modified.changed_files == 1 and modified.snapshot_id != cold.snapshot_id
            (root / "docs/design.md").unlink()
            deleted = service.index()
            assert deleted.deleted_files == 1 and deleted.files == 1
            source.write_text(source.read_text(encoding="utf-8") + "\ndef committed():\n    return True\n", encoding="utf-8")

            def crash(stage):
                if stage == "before_publish":
                    raise RuntimeError("demo crash before publication")

            try:
                service.index(fault=crash)
            except RuntimeError:
                pass
            else:
                raise AssertionError("the injected crash did not occur")
            assert service.status()["snapshot_id"] == deleted.snapshot_id
            resumed = service.index()
            assert resumed.embedding_requests == 0
            hits = service.search("reconcile", mode="hybrid")
            assert hits.hits and all(hit.snapshot_id == resumed.snapshot_id for hit in hits.hits)
            return {"mode": "offline_fixture", "cold": cold.to_dict(), "warm": warm.to_dict(),
                    "modified": modified.to_dict(), "deleted": deleted.to_dict(), "recovered": resumed.to_dict(),
                    "sources": [hit.citation.render() for hit in hits.hits], "budget": ledger.snapshot()}


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
