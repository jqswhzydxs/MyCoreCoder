"""Count every embedding batch, query and retry in the shared workflow ledger."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass

from ..integrations.model_gateway import ModelGateway, classify_error
from ..workflows.budget_ledger import BudgetExceeded, BudgetLedger, TokenRates
from .embeddings import EmbeddingBatch, EmbeddingProvider
from .models import KnowledgeError


@dataclass(frozen=True)
class EmbeddingCall:
    operation: str
    attempt: int
    batch_size: int
    dimensions: int
    duration_ms: float
    category: str | None
    usage_available: bool


def validate_vectors(vectors, *, count: int, dimensions: int) -> tuple[tuple[float, ...], ...]:
    if not isinstance(vectors, (list, tuple)) or len(vectors) != count:
        raise KnowledgeError("invalid_embedding", "embedding result count does not match the request")
    normalized = []
    for vector in vectors:
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
            raise KnowledgeError("invalid_embedding", "embedding vector dimensions changed")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise KnowledgeError("invalid_embedding", "embedding vectors must contain finite numbers")
        normalized.append(tuple(float(value) for value in vector))
    return tuple(normalized)


class EmbeddingGateway:
    def __init__(self, provider: EmbeddingProvider, ledger: BudgetLedger | None = None, *, rates: TokenRates | None = None,
                 max_attempts: int = 3, batch_size: int = 32, max_text_bytes: int = 128_000,
                 max_batch_bytes: int = 256_000, sleep=time.sleep):
        if not 1 <= batch_size <= 32 or not 1 <= max_attempts <= 5 or max_text_bytes < 1 or max_batch_bytes < max_text_bytes:
            raise ValueError("invalid embedding batch/retry limits")
        self.provider = provider
        self.space = provider.describe()
        self.ledger = ledger or BudgetLedger()
        self.rates = rates
        self.max_attempts = max_attempts
        self.batch_size = batch_size
        self.max_text_bytes = max_text_bytes
        self.max_batch_bytes = max_batch_bytes
        self._sleep = sleep
        self.records: list[EmbeddingCall] = []

    def batches(self, texts: list[str]) -> list[list[str]]:
        batches, current, size = [], [], 0
        for text in texts:
            if not isinstance(text, str) or not text:
                raise ValueError("embedding inputs must be nonempty text")
            length = len(text.encode("utf-8"))
            if length > self.max_text_bytes:
                raise KnowledgeError("embedding_input_limit", "a source chunk exceeds the embedding text limit")
            if current and (len(current) >= self.batch_size or size + length > self.max_batch_bytes):
                batches.append(current)
                current, size = [], 0
            current.append(text)
            size += length
        if current:
            batches.append(current)
        return batches

    def _invoke(self, texts: list[str], operation: str) -> EmbeddingBatch:
        if len(self.batches(texts)) != 1:
            raise ValueError("embed_batch requires a single bounded batch")
        self.ledger.check()
        prepare = getattr(self.provider, "validate_configuration", None)
        if callable(prepare):
            prepare()
        estimate = sum(len(text.encode("utf-8")) for text in texts)
        for attempt in range(1, self.max_attempts + 1):
            reservation = self.ledger.reserve(estimate, 0, self.rates)
            started = time.monotonic()
            try:
                if self.provider.describe() != self.space:
                    raise KnowledgeError("invalid_embedding", "embedding provider changed vector spaces")
                provider = self.provider
                if callable(getattr(provider, "with_timeout", None)):
                    provider = provider.with_timeout(min(30.0, self.ledger.remaining_seconds()))
                result = provider.embed_query(texts[0]) if operation == "query" else provider.embed_documents(texts)
                if not isinstance(result, EmbeddingBatch):
                    raise KnowledgeError("invalid_embedding", "embedding provider returned an invalid response type")
                vectors = validate_vectors(result.vectors, count=len(texts), dimensions=self.space.dimensions)
            except Exception as exc:
                self.ledger.settle(reservation, rates=self.rates)
                category = exc.category if isinstance(exc, KnowledgeError) else classify_error(exc).category.value
                self.records.append(EmbeddingCall(operation, attempt, len(texts), self.space.dimensions,
                                                  (time.monotonic() - started) * 1000, category, False))
                if isinstance(exc, KnowledgeError):
                    raise exc from None
                failure = classify_error(exc)
                if not failure.retryable or attempt == self.max_attempts:
                    raise KnowledgeError("embedding_unavailable", f"embedding request failed: {category}") from None
                delay = ModelGateway._retry_delay(exc, attempt)
                if delay >= self.ledger.remaining_seconds():
                    raise BudgetExceeded("workflow deadline prevents embedding retry") from None
                self._sleep(delay)
                continue
            known = isinstance(result.input_tokens, int) and not isinstance(result.input_tokens, bool) and result.input_tokens >= 0
            self.ledger.settle(reservation, prompt_tokens=result.input_tokens if known else None,
                               completion_tokens=0 if known else None, rates=self.rates)
            self.records.append(EmbeddingCall(operation, attempt, len(texts), self.space.dimensions,
                                              (time.monotonic() - started) * 1000, None, known))
            self.ledger.check()
            return EmbeddingBatch(vectors, result.input_tokens if known else None)
        raise AssertionError("unreachable")

    def embed_batch(self, texts: list[str]) -> EmbeddingBatch:
        return self._invoke(texts, "document")

    def embed_documents(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(vector for batch in self.batches(texts) for vector in self.embed_batch(batch).vectors)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._invoke([text], "query").vectors[0]

    def metrics(self) -> dict:
        return {"calls": [asdict(record) for record in self.records], "budget": self.ledger.snapshot()}
