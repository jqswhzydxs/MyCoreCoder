"""Cold local indexing, publication and answers without any embedding backend."""

import importlib.util
import json
from unittest.mock import Mock

import pytest

from corecoder.knowledge.cli import main
from corecoder.knowledge.manifest_store import ManifestStore
from corecoder.knowledge.models import KnowledgeError, SnapshotManifest
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.knowledge.settings import KnowledgeSettings

pytestmark = pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("pathspec", "langchain_text_splitters")),
    reason="local knowledge loader/splitter dependencies are absent",
)


def repository(root):
    (root / "corecoder").mkdir()
    (root / "docs").mkdir()
    (root / "corecoder/run_store.py").write_text(
        'class RunStore:\n    """恢复任务并记录工具事实。"""\n'
        '    def resume_run(self):\n        return "reconciled"\n', encoding="utf-8")
    (root / "docs/recovery.md").write_text("# 恢复任务\nRunStore 使用日志恢复任务，避免重复副作用。\n", encoding="utf-8")
    return create_scope(root)


@pytest.mark.parametrize("configured_default", [False, True])
def test_cli_cold_local_index_and_search_never_construct_a_model_or_vector_store(
    tmp_path, monkeypatch, capsys, configured_default,
):
    scope = repository(tmp_path)
    if configured_default:
        (tmp_path / "corecoder.toml").write_text(
            '[knowledge]\nretrieval_mode="lexical"\n[embedding]\nmodel="unused-remote-model"\n', encoding="utf-8")
    unavailable = Mock(side_effect=AssertionError("local operations must not construct a model/vector backend"))
    monkeypatch.setattr(KnowledgeSettings, "gateway", unavailable)
    monkeypatch.setattr(KnowledgeService, "_vector_store", unavailable)
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name in {"chromadb", "langchain_chroma"} else find_spec(name))
    mode_args = [] if configured_default else ["--mode", "lexical"]
    assert main(["index", "--root", str(tmp_path), *mode_args, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "lexical" and result["files"] == 2
    assert result["embedding_requests"] == result["embedded_texts"] == result["budget"]["requests"] == 0
    manifest = ManifestStore(scope).manifest()
    assert manifest.embedding_space is None
    assert SnapshotManifest.from_dict(manifest.to_dict()) == manifest
    assert not (scope.data_root / "vectors").exists()
    assert main(["search", "RunStore 如何恢复任务", "--root", str(tmp_path), *mode_args, "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["mode"] == "lexical" and found["hits"] and not found["degraded"]
    assert found["budget"]["requests"] == 0
    assert {hit["citation"]["source_path"] for hit in found["hits"]} <= {"corecoder/run_store.py", "docs/recovery.md"}
    unavailable.assert_not_called()


def test_local_incremental_index_handles_rename_delete_freshness_and_cleanup(tmp_path, monkeypatch):
    scope = repository(tmp_path)
    monkeypatch.setattr(KnowledgeService, "_vector_store", Mock(side_effect=AssertionError("no vector backend")))
    with KnowledgeService(scope) as service:
        first = service.index(mode="lexical")
        warm = service.index(mode="lexical")
        assert first.snapshot_id == warm.snapshot_id and warm.reused_files == 2
        path = tmp_path / "docs/recovery.md"
        path.write_text("# 新恢复策略\nRunStore 先核对日志，再恢复任务。\n", encoding="utf-8")
        assert all(hit.chunk.source_path != "docs/recovery.md" for hit in service.search("RunStore", mode="lexical").hits)
        changed = service.index(mode="lexical")
        assert changed.changed_files == changed.reused_files == 1
        path.rename(tmp_path / "docs/renamed.md")
        renamed = service.index(mode="lexical")
        assert renamed.deleted_files == 1 and renamed.embedding_requests == 0
        assert any(hit.chunk.source_path == "docs/renamed.md" for hit in service.search("新恢复策略", mode="lexical").hits)
        (tmp_path / "docs/renamed.md").unlink()
        deleted = service.index(mode="lexical")
        assert deleted.deleted_files == 1 and deleted.files == 1
        store = ManifestStore(scope)
        with store.pin(first.snapshot_id):
            service.cleanup(keep_retired=0, grace_seconds=0)
            assert store.manifest(first.snapshot_id)
        assert service.cleanup(keep_retired=0, grace_seconds=0)["removed_snapshots"] == 1
        assert service.status()["snapshot_id"] == deleted.snapshot_id
        assert service.search("RunStore", mode="hybrid").degraded == ("embedding_unavailable",)
        with pytest.raises(KnowledgeError, match="embedding"):
            service.search("RunStore", mode="vector")


@pytest.mark.parametrize("stage", ["after_fts", "before_publish", "after_publish"])
def test_local_publication_faults_preserve_a_complete_active_snapshot(tmp_path, stage):
    scope = repository(tmp_path)
    with KnowledgeService(scope) as service:
        first = service.index(mode="lexical")
        (tmp_path / "docs/new.md").write_text("# 新文档\nRunStore 新证据。\n", encoding="utf-8")

        def fault(current):
            if current == stage:
                raise RuntimeError("injected publication fault")

        with pytest.raises(RuntimeError, match="injected"):
            service.index(mode="lexical", fault=fault)
        assert (service.status()["snapshot_id"] != first.snapshot_id) == (stage == "after_publish")
        assert service.search("RunStore", mode="lexical").hits
        recovered = service.index(mode="lexical")
        assert recovered.files == 3 and recovered.embedding_requests == 0


def test_local_answer_refreshes_changed_sources_without_embeddings(tmp_path):
    from corecoder.integrations.model_gateway import ModelGateway
    from corecoder.knowledge.answering import FixtureAnswerBackend, RepositoryAnswerer

    scope = repository(tmp_path)
    with KnowledgeService(scope) as service:
        first = service.index(mode="lexical")
        (tmp_path / "corecoder/run_store.py").write_text(
            'class RunStore:\n    """RunStore 使用持久化日志恢复任务。"""\n', encoding="utf-8")
        gateway = ModelGateway(FixtureAnswerBackend())
        result = RepositoryAnswerer(service, gateway, mode="lexical").ask("RunStore")
        assert result.status == "answered" and result.sources
        assert result.snapshot_id != first.snapshot_id
        assert ManifestStore(scope).manifest().embedding_space is None
        assert service.embeddings is None
