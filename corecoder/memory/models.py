"""Versioned, controller-owned memory records and source attestations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

SCHEMA_VERSION = 1
STATUSES = frozenset({"candidate", "active", "superseded", "expired", "deleted"})


class MemoryError(RuntimeError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory time must have a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def bounded(value: str, maximum: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise MemoryError("invalid_request", f"invalid memory {name}")
    return value.strip()


def memory_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", value):
        raise MemoryError("invalid_request", "memory key must use lowercase identifiers")
    return value


@dataclass(frozen=True)
class SourceAuthority:
    """Constructed by trusted application code, never from model output."""

    kind: str
    source_ref: str
    source_hash: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    owner_id: str
    project_id: str
    kind: str
    key: str
    content: str
    source_kind: str
    source_ref: str
    source_hash: str
    evidence_refs: tuple[str, ...]
    status: str
    created_at: str
    expires_at: str | None
    supersedes: str | None
    write_operation_id: str
    stale_reason: str | None = None
    deleted_at: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def prompt_data(self) -> dict:
        return {"id": self.memory_id, "key": self.key, "content": self.content,
                "source": {"kind": self.source_kind, "ref": self.source_ref, "hash": self.source_hash},
                "created_at": self.created_at, "expires_at": self.expires_at,
                "authority": "historical_reference_only"}


@dataclass(frozen=True)
class RecallResult:
    memories: tuple[MemoryRecord, ...]
    excluded: tuple[dict, ...]

    def prompt_data(self) -> list[dict]:
        return [record.prompt_data() for record in self.memories]

    def constraints(self) -> list[dict]:
        return [{"type": "historical_memory", **item} for item in self.prompt_data()]
