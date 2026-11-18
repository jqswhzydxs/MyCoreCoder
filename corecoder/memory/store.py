"""SQLite transactions, scoped idempotency and content-free deletion tombstones."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..knowledge.models import ProjectScope, digest
from .models import SCHEMA_VERSION, MemoryError, MemoryRecord


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, SCHEMA_VERSION}:
                raise MemoryError("incompatible_store", "memory schema is incompatible; preserve the database")
            db.execute("CREATE TABLE IF NOT EXISTS memory_projects (owner TEXT, project TEXT, root_hash TEXT NOT NULL, "
                       "PRIMARY KEY(owner,project))")
            db.execute("""CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
                kind TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
                source_kind TEXT NOT NULL, source_ref TEXT NOT NULL, source_hash TEXT NOT NULL,
                evidence_json TEXT NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('candidate','active','superseded','expired','deleted')),
                created_at TEXT NOT NULL, expires_at TEXT, supersedes TEXT,
                operation_id TEXT NOT NULL, stale_reason TEXT, deleted_at TEXT, schema_version INTEGER NOT NULL,
                FOREIGN KEY(owner,project) REFERENCES memory_projects(owner,project))""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS memory_active_key ON memories(owner,project,key) WHERE status='active'")
            db.execute("""CREATE TABLE IF NOT EXISTS memory_operations (
                owner TEXT, project TEXT, operation_id TEXT, request_hash TEXT NOT NULL,
                memory_id TEXT NOT NULL REFERENCES memories(memory_id), PRIMARY KEY(owner,project,operation_id))""")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db:
                db.rollback()
            raise MemoryError("storage_unavailable", "memory storage transaction failed") from None
        finally:
            if db:
                db.close()

    @staticmethod
    def _bind(db, scope):
        root_hash = digest(str(scope.root))
        db.execute("INSERT OR IGNORE INTO memory_projects VALUES (?,?,?)", (scope.owner_id, scope.project_id, root_hash))
        row = db.execute("SELECT root_hash FROM memory_projects WHERE owner=? AND project=?",
                         (scope.owner_id, scope.project_id)).fetchone()
        if row[0] != root_hash:
            raise MemoryError("scope_mismatch", "memory project is bound to a different root")

    @staticmethod
    def _record(row) -> MemoryRecord:
        try:
            if row["schema_version"] != SCHEMA_VERSION:
                raise MemoryError("incompatible_store", "memory record schema is incompatible")
            return MemoryRecord(row["memory_id"], row["owner"], row["project"], row["kind"], row["key"], row["content"],
                                row["source_kind"], row["source_ref"], row["source_hash"], tuple(json.loads(row["evidence_json"])),
                                row["status"], row["created_at"], row["expires_at"], row["supersedes"], row["operation_id"],
                                row["stale_reason"], row["deleted_at"], row["schema_version"])
        except (ValueError, TypeError, KeyError):
            raise MemoryError("corrupt_store", "memory record cannot be decoded") from None

    def write(self, scope: ProjectScope, record: MemoryRecord, request_hash: str) -> MemoryRecord:
        with self.transaction() as db:
            self._bind(db, scope)
            old = db.execute("SELECT request_hash,memory_id FROM memory_operations WHERE owner=? AND project=? AND operation_id=?",
                             (scope.owner_id, scope.project_id, record.write_operation_id)).fetchone()
            if old:
                if old[0] != request_hash:
                    raise MemoryError("operation_conflict", "memory operation was already used with different input")
                return self._record(db.execute("SELECT * FROM memories WHERE memory_id=?", (old[1],)).fetchone())
            prior = None
            if record.status == "active":
                prior = db.execute("SELECT memory_id FROM memories WHERE owner=? AND project=? AND key=? AND status='active'",
                                   (scope.owner_id, scope.project_id, record.key)).fetchone()
                if prior:
                    db.execute("UPDATE memories SET status='superseded' WHERE memory_id=?", (prior[0],))
            record = replace(record, supersedes=prior[0] if prior else None)
            db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (record.memory_id, scope.owner_id, scope.project_id, record.kind, record.key, record.content,
                        record.source_kind, record.source_ref, record.source_hash, json.dumps(record.evidence_refs),
                        record.status, record.created_at, record.expires_at, record.supersedes, record.write_operation_id,
                        record.stale_reason, record.deleted_at, SCHEMA_VERSION))
            db.execute("INSERT INTO memory_operations VALUES (?,?,?,?,?)",
                       (scope.owner_id, scope.project_id, record.write_operation_id, request_hash, record.memory_id))
            return record

    def records(self, scope: ProjectScope, now: str) -> tuple[MemoryRecord, ...]:
        with self.transaction() as db:
            self._bind(db, scope)
            db.execute("UPDATE memories SET status='expired' WHERE owner=? AND project=? AND status IN ('active','candidate') "
                       "AND expires_at IS NOT NULL AND expires_at<=?", (scope.owner_id, scope.project_id, now))
            rows = db.execute("SELECT * FROM memories WHERE owner=? AND project=? ORDER BY created_at DESC,memory_id",
                              (scope.owner_id, scope.project_id)).fetchall()
            return tuple(self._record(row) for row in rows)

    def get(self, scope: ProjectScope, memory_id: str, now: str) -> MemoryRecord:
        for record in self.records(scope, now):
            if record.memory_id == memory_id:
                return record
        raise MemoryError("memory_missing", "memory does not exist in this project")

    def set_stale(self, scope: ProjectScope, memory_id: str, reason: str | None) -> None:
        with self.transaction() as db:
            db.execute("UPDATE memories SET stale_reason=? WHERE owner=? AND project=? AND memory_id=? AND status='active'",
                       (reason, scope.owner_id, scope.project_id, memory_id))

    def activate(self, scope: ProjectScope, memory_id: str, now: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM memories WHERE owner=? AND project=? AND memory_id=?",
                             (scope.owner_id, scope.project_id, memory_id)).fetchone()
            if not row or row["status"] != "candidate":
                return
            if row["expires_at"] is not None and row["expires_at"] <= now:
                db.execute("UPDATE memories SET status='expired' WHERE memory_id=?", (memory_id,))
                return
            prior = db.execute("SELECT memory_id FROM memories WHERE owner=? AND project=? AND key=? AND status='active'",
                               (scope.owner_id, scope.project_id, row["key"])).fetchone()
            if prior:
                db.execute("UPDATE memories SET status='superseded' WHERE memory_id=?", (prior[0],))
            db.execute("UPDATE memories SET status='active',supersedes=? WHERE memory_id=?",
                       (prior[0] if prior else None, memory_id))

    def forget(self, scope: ProjectScope, memory_id: str, now: str) -> MemoryRecord:
        with self.transaction() as db:
            changed = db.execute("""UPDATE memories SET kind='',key='',content='',source_kind='',source_ref='',source_hash='',
                evidence_json='[]',status='deleted',expires_at=NULL,supersedes=NULL,stale_reason=NULL,
                deleted_at=COALESCE(deleted_at,?) WHERE owner=? AND project=? AND memory_id=?""",
                                 (now, scope.owner_id, scope.project_id, memory_id)).rowcount
            if not changed:
                raise MemoryError("memory_missing", "memory does not exist in this project")
            return self._record(db.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone())
