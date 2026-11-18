"""Versioned repository evidence contracts, independent of optional libraries."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
LEXICAL_VERSION = "identifiers-cjk-bigrams-v1"


def _sha256(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


class KnowledgeError(RuntimeError):
    """Safe, content-free failure for CLI and external adapters."""

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in ("\\", ":", "\x00")):
        raise ValueError("source paths must be relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"..", "."} for part in value.split("/")) or "" in value.split("/"):
        raise ValueError("source paths must stay inside the project")
    return path.as_posix()


@dataclass(frozen=True)
class ProjectScope:
    owner_id: str
    project_id: str
    root: Path
    data_root: Path
    directories: tuple[str, ...] = ("corecoder", "docs", "tests")

    def __post_init__(self):
        if not self.owner_id or not self.project_id or not self.root.is_absolute() or not self.data_root.is_absolute():
            raise ValueError("scope requires trusted identities and absolute canonical directories")
        for directory in self.directories:
            relative_path(directory)

    def identity(self) -> dict:
        return {"owner_id": self.owner_id, "project_id": self.project_id}


@dataclass(frozen=True)
class SourceDocument:
    source_path: str
    text: str
    raw_hash: str
    normalized_hash: str
    language: str
    source_kind: str
    size_bytes: int

    def __post_init__(self):
        relative_path(self.source_path)
        if not _sha256(self.raw_hash) or self.size_bytes < 0 or self.normalized_hash != text_hash(self.text):
            raise ValueError("invalid document content metadata")


@dataclass(frozen=True)
class SplitterConfig:
    chunk_size: int = 1800
    chunk_overlap: int = 200

    def __post_init__(self):
        if not 100 <= self.chunk_size <= 20_000 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("invalid character splitter limits")

    @property
    def version(self) -> str:
        return "ast-md-lines-v2-" + digest(asdict(self))[:16]


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    project_id: str
    source_path: str
    start_line: int
    end_line: int
    text: str
    raw_hash: str
    normalized_hash: str
    splitter_version: str
    language: str
    source_kind: str
    symbol: str = ""
    heading: tuple[str, ...] = ()
    fallback: bool = False

    def __post_init__(self):
        relative_path(self.source_path)
        if (not isinstance(self.start_line, int) or isinstance(self.start_line, bool)
                or not isinstance(self.end_line, int) or isinstance(self.end_line, bool)
                or self.start_line < 1 or self.end_line < self.start_line):
            raise ValueError("chunk line ranges are one-based closed intervals")
        if len(self.text.splitlines()) != self.end_line - self.start_line + 1:
            raise ValueError("chunk text must match a complete contiguous line span")
        expected = digest([self.project_id, self.source_path, self.raw_hash, self.start_line, self.end_line, self.splitter_version])
        if self.chunk_id != expected:
            raise ValueError("chunk identity does not match its provenance")
        if not _sha256(self.raw_hash) or not _sha256(self.normalized_hash):
            raise ValueError("chunk file hashes must be SHA-256 identifiers")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> Chunk:
        return cls(**{**value, "heading": tuple(value.get("heading", ()))})


@dataclass(frozen=True)
class EmbeddingSpace:
    provider: str
    model_revision: str
    dimensions: int
    preprocessing_version: str = "normalized-utf8-v1"

    def __post_init__(self):
        if (not self.provider or not self.model_revision or isinstance(self.dimensions, bool)
                or not isinstance(self.dimensions, int) or not 1 <= self.dimensions <= 65536):
            raise ValueError("embedding space requires a model revision and positive dimensions")

    @property
    def space_id(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_id: str
    owner_id: str
    project_id: str
    embedding_space: EmbeddingSpace | None
    splitter_version: str
    files: tuple[dict, ...]
    chunk_ids: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION
    lexical_version: str = LEXICAL_VERSION

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION or self.lexical_version != LEXICAL_VERSION:
            raise ValueError("unsupported manifest schema or lexical preprocessing version")
        for source in self.files:
            relative_path(source["source_path"])
            if not _sha256(source["raw_hash"]) or not _sha256(source["normalized_hash"]):
                raise ValueError("invalid source inventory hashes")
        if len({source["source_path"] for source in self.files}) != len(self.files) or len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("manifest inventories cannot contain duplicate identifiers")
        expected = digest([self.schema_version, self.lexical_version, {"owner_id": self.owner_id, "project_id": self.project_id},
                           asdict(self.embedding_space) if self.embedding_space is not None else None,
                           self.splitter_version, self.files, self.chunk_ids])
        if self.snapshot_id != expected or any(not _sha256(chunk_id) for chunk_id in self.chunk_ids):
            raise ValueError("snapshot identity does not match its complete inventory")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> SnapshotManifest:
        space = value["embedding_space"]
        return cls(**{**value, "embedding_space": EmbeddingSpace(**space) if space is not None else None,
                      "files": tuple(value["files"]), "chunk_ids": tuple(value["chunk_ids"])})


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    snapshot_id: str
    source_path: str
    start_line: int
    end_line: int
    raw_hash: str

    def __post_init__(self):
        relative_path(self.source_path)
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("invalid citation span")

    def render(self) -> str:
        return f"[{self.source_path}:L{self.start_line}-L{self.end_line} @ {self.snapshot_id}]"


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    snapshot_id: str
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None

    @property
    def citation(self) -> Citation:
        chunk = self.chunk
        return Citation(chunk.chunk_id, self.snapshot_id, chunk.source_path, chunk.start_line, chunk.end_line, chunk.raw_hash)

    def to_dict(self) -> dict:
        return {"chunk_id": self.chunk.chunk_id, "text": self.chunk.text, "symbol": self.chunk.symbol,
                "score": self.score, "lexical_rank": self.lexical_rank, "vector_rank": self.vector_rank,
                "citation": asdict(self.citation)}


@dataclass(frozen=True)
class SearchResult:
    snapshot_id: str
    requested_mode: str
    mode: str
    hits: tuple[SearchHit, ...]
    degraded: tuple[str, ...] = ()
    excluded: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {"snapshot_id": self.snapshot_id, "requested_mode": self.requested_mode, "mode": self.mode,
                "degraded": list(self.degraded), "excluded": list(self.excluded), "hits": [hit.to_dict() for hit in self.hits]}


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    hit: SearchHit
    visible_text: str | None = None
    visible_end_line: int | None = None

    @property
    def citation(self) -> Citation:
        original = self.hit.citation
        return Citation(original.chunk_id, original.snapshot_id, original.source_path, original.start_line,
                        self.visible_end_line or original.end_line, original.raw_hash)

    def prompt_data(self) -> dict:
        citation = self.citation
        return {"id": self.id, "text": self.visible_text if self.visible_text is not None else self.hit.chunk.text, "source_path": citation.source_path,
                "start_line": citation.start_line, "end_line": citation.end_line, "snapshot_id": citation.snapshot_id,
                "symbol": self.hit.chunk.symbol, "heading": list(self.hit.chunk.heading)}


@dataclass(frozen=True)
class EvidenceBundle:
    query_id: str
    owner_id: str
    project_id: str
    snapshot_id: str
    evidence: tuple[EvidenceItem, ...]
    mode: str
    degraded: tuple[str, ...] = ()
    excluded: tuple[dict, ...] = ()


@dataclass
class LoadReport:
    documents: list[SourceDocument] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


@dataclass(frozen=True)
class IndexReport:
    snapshot_id: str
    files: int
    chunks: int
    changed_files: int
    deleted_files: int
    reused_files: int
    embedded_texts: int
    embedding_requests: int
    skipped: dict[str, int]
    duration_ms: float
    mode: str

    def to_dict(self) -> dict:
        return asdict(self)
