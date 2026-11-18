"""Reciprocal rank fusion and the optional LangChain BaseRetriever adapter."""

from __future__ import annotations


def reciprocal_rank_fusion(lexical: list[str], vector: list[str], *, constant: int = 60) -> dict[str, tuple[float, int | None, int | None]]:
    if constant < 1:
        raise ValueError("RRF constant must be positive")
    ranks = [{chunk_id: rank for rank, chunk_id in enumerate(dict.fromkeys(ids), 1)} for ids in (lexical, vector)]
    return {chunk_id: (sum(1 / (constant + ranking[chunk_id]) for ranking in ranks if chunk_id in ranking),
                       ranks[0].get(chunk_id), ranks[1].get(chunk_id)) for chunk_id in set(lexical) | set(vector)}


def knowledge_retriever(service, *, mode: str = "hybrid", top_k: int = 6):
    from typing import Any

    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
    from pydantic import ConfigDict, Field, PrivateAttr

    class RepositoryRetriever(BaseRetriever):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        knowledge_service: Any = Field(exclude=True)
        retrieval_mode: str
        result_count: int
        _last_result: Any = PrivateAttr(default=None)

        @property
        def last_result(self):
            return self._last_result

        def _get_relevant_documents(self, query: str, *, run_manager):
            self._last_result = self.knowledge_service.search(query, mode=self.retrieval_mode, top_k=self.result_count)
            return [Document(page_content=hit.chunk.text, metadata={"chunk_id": hit.chunk.chunk_id, "snapshot_id": hit.snapshot_id,
                            "source_path": hit.chunk.source_path, "start_line": hit.chunk.start_line, "end_line": hit.chunk.end_line,
                            "raw_hash": hit.chunk.raw_hash, "lexical_rank": hit.lexical_rank, "vector_rank": hit.vector_rank})
                    for hit in self._last_result.hits]

    return RepositoryRetriever(knowledge_service=service, retrieval_mode=mode, result_count=top_k)
