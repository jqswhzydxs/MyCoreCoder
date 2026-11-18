"""Scoped local payloads with purpose, expiry and immutable content hashes."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ..knowledge.models import ProjectScope, digest
from ..memory.models import MemoryError
from ..memory.policy import check_content
from .state import WorkflowError

PURPOSES = frozenset({"request", "retrieval", "memory", "plan", "result"})


class PayloadStore:
    def __init__(self, path: str | Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise WorkflowError("incompatible_store", "workflow payload schema is incompatible")
            db.execute("""CREATE TABLE IF NOT EXISTS payloads (
                ref TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL, root_hash TEXT NOT NULL,
                purpose TEXT NOT NULL, body TEXT NOT NULL, content_hash TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, schema_version INTEGER NOT NULL,lifecycle_id TEXT NOT NULL)""")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db:
                db.rollback()
            raise WorkflowError("storage_unavailable", "workflow payload transaction failed") from None
        finally:
            if db:
                db.close()

    def put(self, scope: ProjectScope, purpose: str, value, *, lifecycle_id: str = "local", ttl_seconds: float = 30 * 86400) -> str:
        if purpose not in PURPOSES or not 0 < ttl_seconds <= 365 * 86400:
            raise ValueError("invalid payload purpose or lifetime")
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        try:
            check_content(body, maximum=1024 * 1024)
        except MemoryError:
            raise WorkflowError("sensitive_payload", "workflow payload contains a possible credential or exceeds the size limit") from None
        content_hash = digest(value)
        ref = digest([scope.identity(), digest(str(scope.root)), purpose, content_hash, lifecycle_id])
        now = self.clock()
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO payloads VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (ref, scope.owner_id, scope.project_id, digest(str(scope.root)), purpose, body, content_hash,
                        now, now + ttl_seconds, 1, lifecycle_id))
        return ref

    def get(self, scope: ProjectScope, ref: str, purpose: str):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM payloads WHERE ref=? AND owner=? AND project=? AND root_hash=? AND purpose=?",
                             (ref, scope.owner_id, scope.project_id, digest(str(scope.root)), purpose)).fetchone()
        if not row or row["expires_at"] <= self.clock():
            raise WorkflowError("needs_action", "required workflow payload is missing, expired or outside this project")
        try:
            value = json.loads(row["body"])
            expected = digest([scope.identity(), digest(str(scope.root)), purpose, digest(value), row["lifecycle_id"]])
            if row["schema_version"] != 1 or digest(value) != row["content_hash"] or expected != ref:
                raise ValueError("payload mismatch")
        except (ValueError, TypeError):
            raise WorkflowError("needs_action", "workflow payload hash or schema is invalid") from None
        return value

    def expire(self, scope: ProjectScope, ref: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM payloads WHERE owner=? AND project=? AND ref=?",
                       (scope.owner_id, scope.project_id, ref))
