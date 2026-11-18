"""Real local Chroma/FTS5 generations, isolation, faults, RRF and citations."""

import json
from dataclasses import replace

import pytest

pytest.importorskip("chromadb")
pytest.importorskip("langchain_chroma")

from corecoder.knowledge.citations import build_evidence, render_sources
from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import FixtureEmbeddingProvider
from corecoder.knowledge.lexical_store import LexicalStore, tokenize
from corecoder.knowledge.manifest_store import ManifestStore
from corecoder.knowledge.models import KnowledgeError, SplitterConfig
from corecoder.knowledge.retriever import reciprocal_rank_fusion
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.knowledge.vector_store import VectorStore
from corecoder.workflows.budget_ledger import BudgetExceeded, BudgetLedger, WorkflowBudget


def repository(root):
    (root / "corecoder").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "corecoder/runtime.py").write_text(
        'class RunStore:\n    """Journal tool execution before reporting completion."""\n'
        '    def resume_run(self):\n        """Recover without duplicate side effects."""\n        return "reconciled"\n', encoding="utf-8")
    (root / "docs/verification.md").write_text("# Verification\n只有验证器证据能确认任务完成，模型文本不能证明成功。\n", encoding="utf-8")
    (root / "docs/recovery.md").write_text("# 恢复\nRunStore 记录工具事实，崩溃后恢复并避免重复副作用。\n", encoding="utf-8")
    return create_scope(root)


def service_for(scope, *, provider=None, ledger=None):
    provider = provider or FixtureEmbeddingProvider(16)
    ledger = ledger or BudgetLedger(WorkflowBudget(max_requests=100, max_tokens=1_000_000))
    return KnowledgeService(scope, EmbeddingGateway(provider, ledger))


def test_cold_reopen_and_zero_change_do_not_reembed(tmp_path):
    scope = repository(tmp_path)
    provider = FixtureEmbeddingProvider(16)
    with service_for(scope, provider=provider) as service:
        first = service.index()
        assert first.files == 3 and first.chunks >= 4 and first.embedding_requests > 0
        before = provider.document_requests
        second = service.index()
        assert second.snapshot_id == first.snapshot_id
        assert second.embedding_requests == second.embedded_texts == 0
        assert provider.document_requests == before
        assert second.reused_files == 3
    with service_for(scope) as reopened:
        assert reopened.status()["snapshot_id"] == first.snapshot_id
        for mode in ("lexical", "vector", "hybrid"):
            result = reopened.search("RunStore", mode=mode)
            assert result.hits and result.mode == mode
            assert all(hit.snapshot_id == first.snapshot_id for hit in result.hits)


def test_modify_delete_and_rename_use_new_paths_and_cached_text(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        first = service.index()
        before = ManifestStore(scope).chunks(first.snapshot_id)
        old_doc = next(chunk for chunk in before if chunk.source_path == "docs/recovery.md")
        path = tmp_path / "corecoder/runtime.py"
        path.write_text(path.read_text(encoding="utf-8") + "\ndef reconcile():\n    return 'checked'\n", encoding="utf-8")
        changed = service.index()
        assert changed.changed_files == 1 and changed.reused_files == 2
        (tmp_path / "docs/recovery.md").rename(tmp_path / "docs/recovery-renamed.md")
        renamed = service.index()
        assert renamed.deleted_files == 1 and renamed.embedding_requests == 0
        current = ManifestStore(scope).chunks(renamed.snapshot_id)
        assert all(chunk.source_path != "docs/recovery.md" for chunk in current)
        assert any(chunk.source_path == "docs/recovery-renamed.md" for chunk in current)
        with pytest.raises(KnowledgeError):
            service.get_chunk(old_doc.chunk_id, renamed.snapshot_id)
        (tmp_path / "docs/recovery-renamed.md").unlink()
        deleted = service.index()
        assert deleted.files == 2 and deleted.deleted_files == 1


@pytest.mark.parametrize("stage", ["before_embedding", "after_embedding", "after_vectors", "after_fts", "before_publish", "after_publish"])
def test_faults_preserve_published_snapshot_and_reuse_completed_cache(tmp_path, stage):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        first = service.index()
        source = tmp_path / "corecoder/runtime.py"
        source.write_text(source.read_text(encoding="utf-8") + "\ndef added():\n    return 'new fact'\n", encoding="utf-8")
        fired = []

        def fault(current):
            if current == stage and not fired:
                fired.append(current)
                raise RuntimeError("injected index crash")

        with pytest.raises(RuntimeError, match="injected index crash"):
            service.index(fault=fault)
        active = service.status()["snapshot_id"]
        assert (active != first.snapshot_id) == (stage == "after_publish")
        assert service.search("Verification", mode="lexical").hits
        recovered = service.index()
        assert recovered.snapshot_id != first.snapshot_id
        if stage != "before_embedding":
            assert recovered.embedding_requests == 0


def test_new_vector_space_and_splitter_version_never_mix_generations(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        original = service.index()
    provider = FixtureEmbeddingProvider(8)
    with service_for(scope, provider=provider) as changed:
        report = changed.index()
        assert report.snapshot_id != original.snapshot_id and report.embedded_texts > 0
        assert changed.search("RunStore", mode="vector").hits
        changed.splitter = SplitterConfig(1000, 100)
        next_report = changed.index()
        assert next_report.snapshot_id != report.snapshot_id


def test_switching_between_local_and_vector_indexes_reuses_separate_vector_cache(tmp_path):
    scope = repository(tmp_path)
    provider = FixtureEmbeddingProvider(16)
    with service_for(scope, provider=provider) as service:
        semantic = service.index()
        before = provider.document_requests
        local = service.index(mode="lexical")
        assert local.snapshot_id != semantic.snapshot_id and local.embedding_requests == 0
        assert provider.document_requests == before
        assert ManifestStore(scope).manifest().embedding_space is None
        queries_before = provider.query_requests
        assert service.search("RunStore", mode="hybrid").mode == "lexical"
        assert provider.query_requests == queries_before
        restored = service.index()
        assert restored.snapshot_id == semantic.snapshot_id and restored.embedding_requests == 0
        assert service.search("RunStore", mode="vector").hits


def test_scope_is_filtered_before_top_k_and_foreign_snapshot_is_rejected(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    shared = tmp_path / "shared-index"
    a, b = replace(repository(one), data_root=shared), replace(repository(two), data_root=shared)
    (two / "docs/bait.md").write_text("RunStore RunStore PRIVATE_OTHER_PROJECT_BAIT", encoding="utf-8")
    with service_for(a) as first, service_for(b) as second:
        report_a, report_b = first.index(), second.index()
        for mode in ("lexical", "vector", "hybrid"):
            results = first.search("RunStore", mode=mode, top_k=1)
            assert results.hits and all(hit.chunk.project_id == a.project_id for hit in results.hits)
            assert "PRIVATE_OTHER_PROJECT_BAIT" not in json.dumps(results.to_dict())
        other = ManifestStore(b).chunks(report_b.snapshot_id)[0]
        with pytest.raises(KnowledgeError):
            first.get_chunk(other.chunk_id, report_b.snapshot_id)
        assert report_a.snapshot_id != report_b.snapshot_id


def test_keyword_identifier_chinese_and_special_query_syntax_are_safe(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        report = service.index()
        lexical = LexicalStore(ManifestStore(scope))
        assert lexical.search(report.snapshot_id, "resume_run")
        assert lexical.search(report.snapshot_id, "重复副作用")
        assert lexical.search(report.snapshot_id, '" OR NEAR(x*) -- )') == []
        assert lexical.search(report.snapshot_id, "!!!") == []
    assert {"runstore", "run", "store", "resume_run", "resume"}.issubset(tokenize("RunStore resume_run"))


def test_hybrid_degradation_is_explicit_and_lexical_does_not_require_embeddings(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        service.index()
    with KnowledgeService(scope) as lexical:
        assert lexical.search("RunStore", mode="lexical").degraded == ()
        result = lexical.search("RunStore", mode="hybrid")
        assert result.mode == "lexical" and result.degraded == ("embedding_unavailable",)
        with pytest.raises(KnowledgeError, match="embedding"):
            lexical.search("RunStore", mode="vector")


def test_query_embedding_budget_exhaustion_cannot_be_hidden_by_fallback(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        service.index()
    ledger = BudgetLedger(WorkflowBudget(max_requests=1))
    with service_for(scope, ledger=ledger) as service:
        assert service.search("RunStore").hits
        with pytest.raises(BudgetExceeded):
            service.search("RunStore")


def test_rrf_uses_one_based_ranks_and_deduplicates_each_channel():
    scores = reciprocal_rank_fusion(["a", "a", "b"], ["b", "c"])
    assert scores["a"] == (1 / 61, 1, None)
    assert scores["b"] == (1 / 62 + 1 / 61, 2, 1)


def test_sources_changed_deleted_or_mixed_cannot_be_shown_as_current(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        service.index()
        results = service.search("RunStore", mode="lexical")
        bundle = build_evidence(scope, "RunStore", results)
        assert bundle.evidence
        sources = render_sources(bundle, [bundle.evidence[0].id])
        assert sources[0]["raw_hash"] == bundle.evidence[0].hit.chunk.raw_hash
        with pytest.raises(KnowledgeError, match="unknown evidence"):
            render_sources(bundle, ["forged-id"])
        with pytest.raises(KnowledgeError, match="mixed snapshots"):
            build_evidence(scope, "RunStore", replace(results, hits=(replace(results.hits[0], snapshot_id="other"),)))
        path = tmp_path / results.hits[0].chunk.source_path
        path.unlink()
        assert not build_evidence(scope, "RunStore", replace(results, hits=(results.hits[0],))).evidence
        with pytest.raises(KnowledgeError):
            service.get_chunk(results.hits[0].chunk.chunk_id, results.snapshot_id)


def test_vector_store_never_calls_an_implicit_embedding_function_and_closes(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        report = service.index()
    space = ManifestStore(scope).manifest().embedding_space
    with VectorStore(scope, space) as vectors:
        assert vectors.ids(report.snapshot_id)
        with pytest.raises(RuntimeError, match="implicit embedding"):
            vectors.langchain.add_texts(["accidental implicit call"])
        vectors.delete_snapshot(report.snapshot_id)
        assert vectors.ids(report.snapshot_id) == set()


def test_cleanup_is_idempotent_preserves_active_and_waits_for_existing_readers(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        first = service.index()
        (tmp_path / "docs/new.md").write_text("# New\nNew evidence\n", encoding="utf-8")
        second = service.index()
        manifests = ManifestStore(scope)
        with manifests.pin(first.snapshot_id):
            assert service.cleanup(keep_retired=0, grace_seconds=0)["removed_snapshots"] == 0
            assert manifests.manifest(first.snapshot_id)
        assert service.cleanup(keep_retired=0, grace_seconds=0)["removed_snapshots"] == 1
        assert service.status()["snapshot_id"] == second.snapshot_id
        assert service.cleanup(keep_retired=0, grace_seconds=0)["removed_snapshots"] == 0
        with pytest.raises(KnowledgeError):
            manifests.manifest(first.snapshot_id)
