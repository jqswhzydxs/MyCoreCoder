"""Incremental complete generations: reuse vectors, stage, validate, publish."""

from __future__ import annotations

import time
from dataclasses import asdict

from .embedding_gateway import EmbeddingGateway
from .lexical_store import LexicalStore
from .loaders import load_sources
from .manifest_store import ManifestStore
from .models import (
    LEXICAL_VERSION,
    SCHEMA_VERSION,
    IndexReport,
    KnowledgeError,
    SnapshotManifest,
    SplitterConfig,
    digest,
    text_hash,
)
from .splitters import split_document


class Indexer:
    def __init__(self, manifests: ManifestStore, embeddings: EmbeddingGateway | None = None, *, splitter: SplitterConfig | None = None,
                 max_file_bytes: int = 1024 * 1024, fault=None):
        self.manifests = manifests
        self.scope = manifests.scope
        self.embeddings = embeddings
        self.splitter = splitter or SplitterConfig()
        self.max_file_bytes = max_file_bytes
        self.fault = fault or (lambda stage: None)

    def run(self) -> IndexReport:
        started = time.monotonic()
        requests_before = len(self.embeddings.records) if self.embeddings else 0
        space = self.embeddings.space if self.embeddings else None
        job = self.manifests.acquire_job()
        embedded = 0
        try:
            loaded = load_sources(self.scope, max_file_bytes=self.max_file_bytes)
            self.manifests.renew(job)
            old_id = self.manifests.active_id()
            old = self.manifests.manifest(old_id) if old_id else None
            old_files = {source["source_path"]: source for source in old.files} if old else {}
            old_chunks = {}
            if old and old.splitter_version == self.splitter.version:
                for chunk in self.manifests.chunks(old.snapshot_id):
                    old_chunks.setdefault(chunk.source_path, []).append(chunk)
            chunks, files, reused, changed = [], [], 0, 0
            for document in loaded.documents:
                previous = old_files.get(document.source_path)
                if previous and previous["raw_hash"] == document.raw_hash and old.splitter_version == self.splitter.version:
                    pieces = old_chunks.get(document.source_path, [])
                    reused += 1
                else:
                    pieces = split_document(self.scope, document, self.splitter)
                    changed += 1
                chunks.extend(pieces)
                files.append({key: value for key, value in asdict(document).items() if key != "text"})
            chunks.sort(key=lambda chunk: (chunk.source_path, chunk.start_line, chunk.end_line, chunk.chunk_id))
            ids = tuple(chunk.chunk_id for chunk in chunks)
            snapshot_id = digest([SCHEMA_VERSION, LEXICAL_VERSION, self.scope.identity(), asdict(space) if space else None,
                                  self.splitter.version, files, ids])
            deleted = len(set(old_files) - {document.source_path for document in loaded.documents})
            manifest = SnapshotManifest(snapshot_id, self.scope.owner_id, self.scope.project_id, space,
                                        self.splitter.version, tuple(files), ids)
            if old_id != snapshot_id:
                self.manifests.stage(job, manifest, chunks)
                if self.embeddings is not None:
                    from .vector_store import VectorStore

                    cache = self.manifests.cache(space)
                    missing = {text_hash(chunk.text): chunk.text for chunk in chunks if text_hash(chunk.text) not in cache}
                    for batch in self.embeddings.batches(list(missing.values())):
                        self.manifests.renew(job)
                        self.fault("before_embedding")
                        result = self.embeddings.embed_batch(batch)
                        values = {text_hash(text): vector for text, vector in zip(batch, result.vectors, strict=True)}
                        self.manifests.put_cache(job, space, values)
                        cache.update(values)
                        embedded += len(batch)
                        self.fault("after_embedding")
                    self.manifests.renew(job)
                    with VectorStore(self.scope, space) as vectors:
                        vectors.write(snapshot_id, chunks, [cache[text_hash(chunk.text)] for chunk in chunks])
                        if vectors.ids(snapshot_id) != {vectors.vector_id(snapshot_id, chunk.chunk_id) for chunk in chunks}:
                            raise KnowledgeError("index_corrupt", "vector inventory does not match the snapshot")
                    self.fault("after_vectors")
                with self.manifests.connect(write=True) as connection:
                    self.manifests._check_job(connection, job)
                    LexicalStore.write_snapshot(connection, self.scope, snapshot_id, chunks)
                self.fault("after_fts")
                self.manifests.ready(job, snapshot_id)
                self.fault("before_publish")
                self.manifests.publish(job, snapshot_id)
                self.fault("after_publish")
            self.manifests.finish_job(job)
            mode = "lexical" if space is None else "fixture" if space.provider == "fixture" else "semantic"
            requests = len(self.embeddings.records) - requests_before if self.embeddings else 0
            return IndexReport(snapshot_id, len(files), len(chunks), changed, deleted, reused, embedded,
                               requests, loaded.skipped, (time.monotonic() - started) * 1000, mode)
        except BaseException as exc:
            category = exc.category if isinstance(exc, KnowledgeError) else "index_interrupted"
            self.manifests.finish_job(job, error_category=category)
            raise
