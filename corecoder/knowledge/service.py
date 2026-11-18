"""Shared repository service for local CLI, workflows and read-only MCP."""

from __future__ import annotations

import sqlite3

from ..workflows.budget_ledger import BudgetExceeded
from .citations import validate_hit
from .embedding_gateway import EmbeddingGateway
from .lexical_store import LexicalStore
from .manifest_store import ManifestStore
from .models import KnowledgeError, ProjectScope, SearchHit, SearchResult, SplitterConfig
from .retriever import reciprocal_rank_fusion


class KnowledgeService:
    def __init__(self, scope: ProjectScope, embeddings: EmbeddingGateway | None = None, *, splitter: SplitterConfig | None = None,
                 max_file_bytes: int = 1024 * 1024, lexical_top_k: int = 20, vector_top_k: int = 20, rrf_constant: int = 60):
        self.scope = scope
        self.embeddings = embeddings
        self.splitter = splitter or SplitterConfig()
        self.max_file_bytes = max_file_bytes
        if not 1 <= lexical_top_k <= 100 or not 1 <= vector_top_k <= 100 or rrf_constant < 1:
            raise ValueError("invalid retrieval limits")
        self.lexical_top_k = lexical_top_k
        self.vector_top_k = vector_top_k
        self.rrf_constant = rrf_constant
        self._vectors = {}

    def _store(self, *, create: bool = False) -> ManifestStore:
        return ManifestStore(self.scope, create=create)

    def status(self) -> dict:
        if not (self.scope.data_root / "metadata.db").exists():
            return {"indexed": False, "snapshot_id": None}
        return self._store().status()

    def preview(self) -> dict:
        from .loaders import load_sources

        loaded = load_sources(self.scope, max_file_bytes=self.max_file_bytes)
        return {"dry_run": True, "files": [document.source_path for document in loaded.documents],
                "file_count": len(loaded.documents), "estimated_input_bytes": sum(document.size_bytes for document in loaded.documents),
                "skipped": loaded.skipped, "embedding_requests": 0}

    def index(self, *, mode: str = "hybrid", fault=None):
        import importlib.util

        from .indexer import Indexer

        if mode not in {"lexical", "vector", "hybrid"}:
            raise KnowledgeError("configuration_error", "invalid index mode")
        dependencies = ("pathspec", "langchain_text_splitters")
        if mode != "lexical":
            dependencies += ("langchain_chroma", "chromadb")
        if any(importlib.util.find_spec(name) is None for name in dependencies):
            raise KnowledgeError("configuration_error", 'indexing requires corecoder[knowledge] on Python 3.11+')
        if mode != "lexical" and self.embeddings is None:
            raise KnowledgeError("configuration_error", "indexing requires a separately configured embedding provider")
        return Indexer(self._store(create=True), self.embeddings if mode != "lexical" else None, splitter=self.splitter,
                       max_file_bytes=self.max_file_bytes, fault=fault).run()

    def _vector_store(self, space):
        from .vector_store import VectorStore

        if space.space_id not in self._vectors:
            self._vectors[space.space_id] = VectorStore(self.scope, space)
        return self._vectors[space.space_id]

    def search(self, query: str, *, mode: str = "hybrid", top_k: int = 6, snapshot_id: str | None = None) -> SearchResult:
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise KnowledgeError("configuration_error", "query must contain 1 to 4000 characters")
        if mode not in {"lexical", "vector", "hybrid"} or isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 50:
            raise KnowledgeError("configuration_error", "invalid search mode or top_k")
        store = self._store()
        with store.pin(snapshot_id) as manifest:
            lexical, vector, degraded = [], [], []
            lexical_available = mode in {"lexical", "hybrid"}
            vector_available = mode in {"vector", "hybrid"}
            if not manifest.chunk_ids:
                return SearchResult(manifest.snapshot_id, mode, mode, ())
            if lexical_available:
                try:
                    lexical = LexicalStore(store).search(manifest.snapshot_id, query, self.lexical_top_k)
                except sqlite3.Error:
                    if mode == "lexical":
                        raise KnowledgeError("index_corrupt", "keyword index is unavailable") from None
                    lexical_available = False
                    degraded.append("lexical_unavailable")
            if vector_available:
                if self.embeddings is None or self.embeddings.space != manifest.embedding_space:
                    if mode == "vector":
                        raise KnowledgeError("embedding_unavailable", "query embedding must match the indexed vector space")
                    vector_available = False
                    degraded.append("embedding_unavailable")
                else:
                    try:
                        query_vector = self.embeddings.embed_query(query)
                        vector = self._vector_store(manifest.embedding_space).search(manifest.snapshot_id, query_vector, top_k=self.vector_top_k)
                    except BudgetExceeded:
                        raise
                    except Exception:  # noqa: BLE001 - report explicit vector degradation without leaking backend text
                        if mode == "vector":
                            raise KnowledgeError("embedding_unavailable", "vector retrieval is unavailable") from None
                        vector_available = False
                        degraded.append("vector_unavailable")
            if not lexical_available and not vector_available:
                raise KnowledgeError("index_unavailable", "no retrieval backend is available")
            effective = "hybrid" if lexical_available and vector_available else "lexical" if lexical_available else "vector"
            fused = reciprocal_rank_fusion(lexical, vector, constant=self.rrf_constant)
            if not set(fused).issubset(manifest.chunk_ids):
                raise KnowledgeError("scope_mismatch", "a retrieval backend returned unauthorized chunk identifiers")
            chunks = {chunk.chunk_id: chunk for chunk in store.chunks(manifest.snapshot_id, list(fused))}
            if set(chunks) != set(fused):
                raise KnowledgeError("index_corrupt", "retrieved chunks are missing from the published inventory")
            ordered = sorted(fused, key=lambda chunk_id: (-fused[chunk_id][0], chunks[chunk_id].source_path,
                                                         chunks[chunk_id].start_line, chunk_id))
            hits, excluded, seen = [], [], set()
            for chunk_id in ordered:
                chunk = chunks[chunk_id]
                score, lexical_rank, vector_rank = fused[chunk_id]
                hit = SearchHit(chunk, manifest.snapshot_id, score, lexical_rank, vector_rank)
                invalid = validate_hit(self.scope, hit, max_file_bytes=self.max_file_bytes)
                if invalid:
                    excluded.append({"chunk_id": chunk_id, "reason": invalid})
                    continue
                key = chunk.source_path, chunk.symbol or "/".join(chunk.heading)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(hit)
                if len(hits) >= top_k:
                    break
            return SearchResult(manifest.snapshot_id, mode, effective, tuple(hits), tuple(degraded), tuple(excluded))

    def get_chunk(self, chunk_id: str, snapshot_id: str):
        store = self._store()
        with store.pin(snapshot_id):
            chunks = store.chunks(snapshot_id, [chunk_id])
            if not chunks:
                raise KnowledgeError("index_missing", "chunk is not present in the requested project snapshot")
            chunk = chunks[0]
            if validate_hit(self.scope, SearchHit(chunk, snapshot_id, 0.0), max_file_bytes=self.max_file_bytes):
                raise KnowledgeError("stale_evidence", "indexed source has changed; rebuild the index before reading this chunk")
            return chunk

    def cleanup(self, *, keep_retired: int = 1, grace_seconds: float = 3600) -> dict:
        store = self._store()
        job = store.acquire_job()
        removed = 0
        try:
            for manifest in store.claim_cleanup(job, keep_retired=keep_retired, grace_seconds=grace_seconds):
                store.renew(job)
                if manifest.embedding_space is not None:
                    self._vector_store(manifest.embedding_space).delete_snapshot(manifest.snapshot_id)
                store.finish_cleanup(job, manifest.snapshot_id)
                removed += 1
            store.finish_job(job)
        except BaseException:
            store.finish_job(job, error_category="cleanup_interrupted")
            raise
        return {"removed_snapshots": removed, "active_snapshot": store.active_id()}

    def close(self) -> None:
        for store in self._vectors.values():
            store.close()
        self._vectors.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
