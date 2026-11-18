"""A thread/turn is bound to one request, configuration and durable budget."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..knowledge.models import digest
from .state import WorkflowError


class WorkflowOperations:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise WorkflowError("incompatible_store", "workflow operation schema is incompatible")
            db.execute("""CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,owner TEXT NOT NULL,project TEXT NOT NULL,root_hash TEXT NOT NULL,
                thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,input_hash TEXT NOT NULL,
                configuration_hash TEXT NOT NULL,request_ref TEXT NOT NULL,budget_initialized INTEGER NOT NULL DEFAULT 0)""")
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
            raise WorkflowError("storage_unavailable", "workflow operation transaction failed") from None
        finally:
            if db:
                db.close()

    def bind(self, scope, *, workflow_id, thread_id, turn_id, input_hash, configuration_hash, request_ref):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO workflows VALUES (?,?,?,?,?,?,?,?,?,0)",
                       (workflow_id, scope.owner_id, scope.project_id, digest(str(scope.root)), thread_id, turn_id,
                        input_hash, configuration_hash, request_ref))
            row = self._get(db, scope, workflow_id)
            if row["input_hash"] != input_hash or row["configuration_hash"] != configuration_hash or row["request_ref"] != request_ref:
                raise WorkflowError("input_mismatch", "workflow turn was already bound to different input or trusted configuration")
            return row

    @staticmethod
    def _get(db, scope, workflow_id):
        row = db.execute("SELECT * FROM workflows WHERE id=? AND owner=? AND project=? AND root_hash=?",
                         (workflow_id, scope.owner_id, scope.project_id, digest(str(scope.root)))).fetchone()
        if row is None:
            raise WorkflowError("needs_action", "workflow does not exist in this project")
        return dict(row)

    def get(self, scope, workflow_id):
        with self.transaction() as db:
            return self._get(db, scope, workflow_id)

    def mark_budget(self, scope, workflow_id):
        with self.transaction() as db:
            self._get(db, scope, workflow_id)
            db.execute("UPDATE workflows SET budget_initialized=1 WHERE id=?", (workflow_id,))
