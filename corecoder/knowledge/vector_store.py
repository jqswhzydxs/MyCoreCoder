"""Explicit local Chroma vectors with LangChain query integration.

No collection has a default embedding function. Writes always include vectors;
queries pass a precomputed vector and an owner/project/snapshot filter.
"""

from __future__ import annotations

import os
import threading

from .embedding_gateway import validate_vectors
from .models import EmbeddingSpace, KnowledgeError, ProjectScope, digest

_CLIENTS: dict[str, tuple[object, int]] = {}
_LOCK = threading.RLock()


class VectorStore:
    def __init__(self, scope: ProjectScope, space: EmbeddingSpace):
        try:
            import chromadb
            from chromadb.config import Settings
            from langchain_chroma import Chroma
            from langchain_core.embeddings import Embeddings
        except ImportError:
            raise KnowledgeError("configuration_error", 'vector indexing requires corecoder[knowledge]') from None

        class NoImplicitEmbeddings(Embeddings):
            def embed_documents(self, texts):
                raise RuntimeError("implicit embedding is disabled; pass explicit vectors")

            def embed_query(self, text):
                raise RuntimeError("implicit embedding is disabled; pass an explicit query vector")

        self.scope = scope
        self.space = space
        self.path = scope.data_root / "vectors"
        self.key = os.path.normcase(str(self.path.resolve()))
        self._closed = False
        with _LOCK:
            if self.key in _CLIENTS:
                self.client, count = _CLIENTS[self.key]
                _CLIENTS[self.key] = self.client, count + 1
            else:
                self.client = chromadb.PersistentClient(path=str(self.path), settings=Settings(anonymized_telemetry=False))
                _CLIENTS[self.key] = self.client, 1
        name = "corecoder-" + space.space_id[:40]
        try:
            self.collection = self.client.get_or_create_collection(name, embedding_function=None, metadata={"hnsw:space": "cosine"})
            self.langchain = Chroma(client=self.client, collection_name=name, embedding_function=NoImplicitEmbeddings())
        except Exception:
            self.close()
            raise

    def _where(self, snapshot_id: str) -> dict:
        return {"$and": [{"owner_id": self.scope.owner_id}, {"project_id": self.scope.project_id}, {"snapshot_id": snapshot_id}]}

    @staticmethod
    def vector_id(snapshot_id: str, chunk_id: str) -> str:
        return digest([snapshot_id, chunk_id])

    def write(self, snapshot_id: str, chunks, vectors) -> None:
        vectors = validate_vectors(vectors, count=len(chunks), dimensions=self.space.dimensions)
        for offset in range(0, len(chunks), 128):
            batch = chunks[offset:offset + 128]
            self.collection.upsert(
                ids=[self.vector_id(snapshot_id, chunk.chunk_id) for chunk in batch],
                embeddings=[list(vector) for vector in vectors[offset:offset + 128]],
                documents=[chunk.text for chunk in batch],
                metadatas=[{**self.scope.identity(), "snapshot_id": snapshot_id, "chunk_id": chunk.chunk_id,
                            "source_path": chunk.source_path, "start_line": chunk.start_line, "end_line": chunk.end_line} for chunk in batch],
            )

    def ids(self, snapshot_id: str) -> set[str]:
        result = self.collection.get(where=self._where(snapshot_id), include=["metadatas"])
        return set(result["ids"])

    def search(self, snapshot_id: str, query_vector: tuple[float, ...], *, top_k: int = 20) -> list[str]:
        vector = validate_vectors([query_vector], count=1, dimensions=self.space.dimensions)[0]
        count = len(self.ids(snapshot_id))
        if not count:
            return []
        documents = self.langchain.similarity_search_by_vector(list(vector), k=min(top_k, count), filter=self._where(snapshot_id))
        return [document.metadata["chunk_id"] for document in documents]

    def delete_snapshot(self, snapshot_id: str) -> None:
        self.collection.delete(where=self._where(snapshot_id))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with _LOCK:
            _, count = _CLIENTS[self.key]
            if count > 1:
                _CLIENTS[self.key] = self.client, count - 1
                return
            del _CLIENTS[self.key]
            close = getattr(self.client, "close", None)
            if callable(close):
                close()
            else:
                # The pinned Chroma release lacks a public local-client close.
                # Its system and cache are process-global; release only our own
                # system after the last adapter owner exits, never all clients.
                from chromadb.api.shared_system_client import SharedSystemClient

                system = self.client._system
                system.stop()
                registry = SharedSystemClient._identifier_to_system
                identifier = self.client._identifier
                if registry.get(identifier) is system:
                    registry.pop(identifier)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
