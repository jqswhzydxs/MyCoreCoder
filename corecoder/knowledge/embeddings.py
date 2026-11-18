"""Independent embedding protocol, real compatible API, and explicit fixture."""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from typing import Protocol

from ..config import EmbeddingConfig
from .models import EmbeddingSpace, KnowledgeError


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int | None = None


class EmbeddingProvider(Protocol):
    def describe(self) -> EmbeddingSpace: ...

    def embed_documents(self, texts: list[str]) -> EmbeddingBatch:
        """Exactly one provider request; no retries inside this boundary."""
        ...

    def embed_query(self, text: str) -> EmbeddingBatch: ...


class OpenAIEmbeddingProvider:
    """A separately configured OpenAI-compatible embedding endpoint.

    Client creation and key lookup are deferred until a request is made.
    This adapter does not load .env or reuse the Chat URL/key.
    """

    def __init__(self, config: EmbeddingConfig, *, dimensions: int = 1536, model_revision: str | None = None,
                 timeout_seconds: float = 30.0, client_factory=None):
        if config.provider != "openai" or not config.model:
            raise KnowledgeError("configuration_error", "configure a separate embedding provider and model")
        self.config = config
        # Passing None to the SDK would inherit OPENAI_BASE_URL, which may
        # point at the unrelated Chat provider. Always bind an explicit URL.
        self.base_url = (config.base_url or "https://api.openai.com/v1").rstrip("/")
        origin = "openai" if self.base_url == "https://api.openai.com/v1" else (
            "openai-compatible-" + hashlib.sha256(self.base_url.encode("utf-8")).hexdigest()[:16])
        self.space = EmbeddingSpace(origin, model_revision or config.model, dimensions)
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory

    def describe(self) -> EmbeddingSpace:
        return self.space

    def validate_configuration(self) -> None:
        if not os.getenv(self.config.api_key_env):
            raise KnowledgeError("configuration_error", "embedding credentials are not configured")

    def with_timeout(self, timeout_seconds: float) -> OpenAIEmbeddingProvider:
        return OpenAIEmbeddingProvider(self.config, dimensions=self.space.dimensions, model_revision=self.space.model_revision,
                                       timeout_seconds=timeout_seconds, client_factory=self.client_factory)

    def embed_documents(self, texts: list[str]) -> EmbeddingBatch:
        from openai import OpenAI

        key = os.getenv(self.config.api_key_env)
        if not key:
            raise KnowledgeError("configuration_error", "embedding credentials are not configured")
        factory = self.client_factory or OpenAI
        with factory(api_key=key, base_url=self.base_url, max_retries=0, timeout=self.timeout_seconds) as client:
            response = client.embeddings.create(model=self.config.model, input=texts, dimensions=self.space.dimensions,
                                                encoding_format="float")
        rows = list(response.data)
        if len(rows) != len(texts) or sorted(row.index for row in rows) != list(range(len(texts))):
            raise KnowledgeError("invalid_embedding", "embedding response indices do not match the batch")
        rows.sort(key=lambda row: row.index)
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "prompt_tokens", None)
        # Compatible text-embedding endpoints may report only total_tokens;
        # embeddings have no generated output tokens, so that is input usage.
        if tokens is None:
            tokens = getattr(usage, "total_tokens", None)
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            tokens = None
        return EmbeddingBatch(tuple(tuple(row.embedding) for row in rows), tokens)

    def embed_query(self, text: str) -> EmbeddingBatch:
        return self.embed_documents([text])


class FixtureEmbeddingProvider:
    """Deterministic hashed token vectors, not a semantic quality model."""

    def __init__(self, dimensions: int = 64):
        self.space = EmbeddingSpace("fixture", "hashed-identifier-bigram-v1", dimensions)
        self.document_requests = 0
        self.query_requests = 0

    def describe(self) -> EmbeddingSpace:
        return self.space

    def _encode(self, text: str) -> tuple[float, ...]:
        from .lexical_store import tokenize

        vector = [0.0] * self.space.dimensions
        for token in tokenize(text) or re.findall(r"\S+", text):
            number = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
            vector[number % len(vector)] += 1.0
        length = math.sqrt(sum(value * value for value in vector))
        if not length:
            vector[0] = length = 1.0
        return tuple(value / length for value in vector)

    def embed_documents(self, texts: list[str]) -> EmbeddingBatch:
        self.document_requests += 1
        return EmbeddingBatch(tuple(self._encode(text) for text in texts))

    def embed_query(self, text: str) -> EmbeddingBatch:
        self.query_requests += 1
        return EmbeddingBatch((self._encode(text),))
