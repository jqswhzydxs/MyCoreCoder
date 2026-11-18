"""Explicit CLI -> environment -> project TOML -> default configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import EmbeddingConfig
from ..workflows.budget_ledger import TokenRates
from .embedding_gateway import EmbeddingGateway
from .embeddings import FixtureEmbeddingProvider, OpenAIEmbeddingProvider
from .models import KnowledgeError, SplitterConfig
from .scope import create_scope


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"1", "true", "yes"}:
        return True
    if str(value).lower() in {"0", "false", "no"}:
        return False
    raise ValueError("boolean configuration is invalid")


@dataclass(frozen=True)
class KnowledgeSettings:
    root: Path
    persist_directory: str = ".corecoder/knowledge"
    directories: tuple[str, ...] = ("corecoder", "docs", "tests")
    include_article: bool = False
    max_file_bytes: int = 1024 * 1024
    chunk_size: int = 1800
    chunk_overlap: int = 200
    retrieval_mode: str = "hybrid"
    lexical_top_k: int = 20
    vector_top_k: int = 20
    context_top_k: int = 6
    context_token_budget: int = 4000
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    dimensions: int = 1536
    model_revision: str | None = None
    embedding_rate: float | None = None
    embedding_batch_size: int = 32
    embedding_max_text_bytes: int = 128_000
    embedding_max_batch_bytes: int = 256_000
    project_config: dict = field(default_factory=dict, repr=False)

    def scope(self):
        return create_scope(self.root, data_root=self.persist_directory, directories=self.directories, include_article=self.include_article)

    def splitter(self) -> SplitterConfig:
        return SplitterConfig(self.chunk_size, self.chunk_overlap)

    def gateway(self, ledger, *, fixture: bool = False, required: bool = True) -> EmbeddingGateway | None:
        provider = "fixture" if fixture else self.embedding.provider
        if provider == "fixture":
            backend = FixtureEmbeddingProvider(self.dimensions)
        elif provider == "openai" and self.embedding.model:
            backend = OpenAIEmbeddingProvider(self.embedding, dimensions=self.dimensions, model_revision=self.model_revision)
        elif not required:
            return None
        else:
            raise KnowledgeError("configuration_error", "configure the separate embedding model or explicitly choose fixture mode")
        rates = TokenRates(self.embedding_rate, 0) if self.embedding_rate is not None else None
        return EmbeddingGateway(backend, ledger, rates=rates, batch_size=self.embedding_batch_size,
                               max_text_bytes=self.embedding_max_text_bytes, max_batch_bytes=self.embedding_max_batch_bytes)


def load_settings(options: dict | None = None) -> KnowledgeSettings:
    options = options or {}
    root = Path(options.get("root") or os.getenv("CORECODER_KNOWLEDGE_ROOT") or Path.cwd()).expanduser().resolve()
    config_path = root / "corecoder.toml"
    project = {}
    if config_path.exists():
        try:
            if config_path.is_symlink() or config_path.stat().st_size > 64 * 1024:
                raise ValueError("invalid project configuration source")
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            project = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ImportError):
            raise KnowledgeError("configuration_error", "project TOML configuration cannot be loaded safely") from None
    knowledge, embedding = project.get("knowledge", {}), project.get("embedding", {})
    if not isinstance(knowledge, dict) or not isinstance(embedding, dict):
        raise KnowledgeError("configuration_error", "knowledge and embedding configuration must be TOML tables")

    def choose(name, env, mapping, default, cast=lambda value: value):
        value = options.get(name)
        if value is None:
            value = os.getenv(env)
        if value is None:
            value = mapping.get(name, default)
        return cast(value)

    try:
        directory_value = choose("directories", "CORECODER_KNOWLEDGE_DIRECTORIES", knowledge, ["corecoder", "docs", "tests"])
        directories = tuple(directory_value.split(",")) if isinstance(directory_value, str) else tuple(directory_value)
        config = EmbeddingConfig(
            provider=choose("provider", "CORECODER_EMBEDDING_PROVIDER", embedding, "openai"),
            model=choose("model", "CORECODER_EMBEDDING_MODEL", embedding, ""),
            base_url=choose("base_url", "CORECODER_EMBEDDING_BASE_URL", embedding, None),
            api_key_env=choose("api_key_env", "CORECODER_EMBEDDING_API_KEY_ENV", embedding, "CORECODER_EMBEDDING_API_KEY"),
        )
        result = KnowledgeSettings(
            root=root,
            persist_directory=choose("persist_directory", "CORECODER_KNOWLEDGE_DATA_ROOT", knowledge, ".corecoder/knowledge"),
            directories=directories,
            include_article=choose("include_article", "CORECODER_KNOWLEDGE_INCLUDE_ARTICLE", knowledge, False, _boolean),
            max_file_bytes=choose("max_file_bytes", "CORECODER_KNOWLEDGE_MAX_FILE_BYTES", knowledge, 1024 * 1024, int),
            chunk_size=choose("chunk_size", "CORECODER_KNOWLEDGE_CHUNK_SIZE", knowledge, 1800, int),
            chunk_overlap=choose("chunk_overlap", "CORECODER_KNOWLEDGE_CHUNK_OVERLAP", knowledge, 200, int),
            retrieval_mode=choose("retrieval_mode", "CORECODER_KNOWLEDGE_MODE", knowledge, "hybrid"),
            lexical_top_k=choose("lexical_top_k", "CORECODER_KNOWLEDGE_LEXICAL_TOP_K", knowledge, 20, int),
            vector_top_k=choose("vector_top_k", "CORECODER_KNOWLEDGE_VECTOR_TOP_K", knowledge, 20, int),
            context_top_k=choose("context_top_k", "CORECODER_KNOWLEDGE_CONTEXT_TOP_K", knowledge, 6, int),
            context_token_budget=choose("context_token_budget", "CORECODER_KNOWLEDGE_CONTEXT_TOKENS", knowledge, 4000, int),
            embedding=config,
            dimensions=choose("dimensions", "CORECODER_EMBEDDING_DIMENSIONS", embedding, 64 if config.provider == "fixture" else 1536, int),
            model_revision=choose("model_revision", "CORECODER_EMBEDDING_MODEL_REVISION", embedding, None),
            embedding_rate=choose("input_per_million", "CORECODER_EMBEDDING_INPUT_PER_MILLION", embedding, None,
                                  lambda value: float(value) if value is not None else None),
            embedding_batch_size=choose("batch_size", "CORECODER_EMBEDDING_BATCH_SIZE", embedding, 32, int),
            embedding_max_text_bytes=choose("max_text_bytes", "CORECODER_EMBEDDING_MAX_TEXT_BYTES", embedding, 128_000, int),
            embedding_max_batch_bytes=choose("max_batch_bytes", "CORECODER_EMBEDDING_MAX_BATCH_BYTES", embedding, 256_000, int),
            project_config=project,
        )
        result.scope()
        result.splitter()
        if (result.retrieval_mode not in {"lexical", "vector", "hybrid"} or not 1 <= result.context_top_k <= 50
                or not 1 <= result.lexical_top_k <= 100 or not 1 <= result.vector_top_k <= 100
                or not 1 <= result.max_file_bytes <= 16 * 1024 * 1024 or result.context_token_budget < 1
                or not 1 <= result.dimensions <= 65536 or not 1 <= result.embedding_batch_size <= 32
                or not 1 <= result.embedding_max_text_bytes <= result.embedding_max_batch_bytes <= 4 * 1024 * 1024):
            raise ValueError("invalid retrieval configuration")
        if result.embedding_rate is not None:
            TokenRates(result.embedding_rate, 0)
        return result
    except (TypeError, ValueError):
        raise KnowledgeError("configuration_error", "knowledge or embedding configuration contains invalid values") from None
