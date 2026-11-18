"""SQLite manifests, stable chunks, embedding cache and transactional publication."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from .models import SCHEMA_VERSION, Chunk, EmbeddingSpace, KnowledgeError, ProjectScope, SnapshotManifest

_SCHEMA = """
CREATE TABLE projects(owner_id TEXT, project_id TEXT, canonical_root TEXT NOT NULL,
    PRIMARY KEY(owner_id,project_id));
CREATE TABLE snapshots(snapshot_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, project_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('building','ready','published','failed','retired')),
    manifest_json TEXT NOT NULL, created_at REAL NOT NULL, published_at REAL, error_category TEXT,
    FOREIGN KEY(owner_id,project_id) REFERENCES projects(owner_id,project_id));
CREATE TABLE source_files(snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    source_path TEXT NOT NULL, raw_hash TEXT NOT NULL, normalized_hash TEXT NOT NULL, details_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,source_path));
CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, chunk_json TEXT NOT NULL);
CREATE TABLE snapshot_chunks(snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id), PRIMARY KEY(snapshot_id,chunk_id));
CREATE TABLE active_snapshots(owner_id TEXT,project_id TEXT,snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    PRIMARY KEY(owner_id,project_id),FOREIGN KEY(owner_id,project_id) REFERENCES projects(owner_id,project_id));
CREATE TABLE index_jobs(job_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,project_id TEXT NOT NULL,token TEXT NOT NULL,
    status TEXT NOT NULL,snapshot_id TEXT,lease_expires REAL NOT NULL,error_category TEXT);
CREATE UNIQUE INDEX one_index_writer ON index_jobs(owner_id,project_id) WHERE status='running';
CREATE TABLE embedding_cache(owner_id TEXT,project_id TEXT,space_id TEXT,text_hash TEXT,vector_json TEXT NOT NULL,
    PRIMARY KEY(owner_id,project_id,space_id,text_hash));
CREATE TABLE snapshot_readers(token TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),expires_at REAL NOT NULL);
CREATE VIRTUAL TABLE chunk_fts USING fts5(owner_id UNINDEXED,project_id UNINDEXED,snapshot_id UNINDEXED,chunk_id UNINDEXED,tokens);
"""


@dataclass(frozen=True)
class IndexJob:
    job_id: str
    token: str


class ManifestStore:
    def __init__(self, scope: ProjectScope, *, create: bool = True, clock=time.time, lease_seconds: float = 300.0):
        self.scope = scope
        self.path = scope.data_root / "metadata.db"
        self.clock = clock
        self.lease_seconds = lease_seconds
        if lease_seconds <= 0:
            raise ValueError("index lease must be positive")
        if not self.path.exists() and not create:
            raise KnowledgeError("index_missing", "no repository index exists; run knowledge index first")
        if self.path.is_symlink():
            raise KnowledgeError("configuration_error", "index metadata cannot be a symbolic link")
        try:
            scope.data_root.mkdir(parents=True, exist_ok=True)
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
            with self.connect(write=True) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise KnowledgeError("index_incompatible", "unversioned index database cannot be opened")
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise KnowledgeError("index_incompatible", "index schema is incompatible; rebuild into a separate data directory")
            with self.connect(write=True) as connection:
                root = os.path.normcase(str(scope.root))
                row = connection.execute("SELECT canonical_root FROM projects WHERE owner_id=? AND project_id=?",
                                         (scope.owner_id, scope.project_id)).fetchone()
                if row and row[0] != root:
                    raise KnowledgeError("scope_mismatch", "index project identity is bound to another canonical root")
                connection.execute("INSERT OR IGNORE INTO projects VALUES(?,?,?)", (scope.owner_id, scope.project_id, root))
        except sqlite3.Error:
            raise KnowledgeError("index_corrupt", "index metadata cannot be initialized or FTS5 is unavailable") from None

    @contextmanager
    def connect(self, *, write: bool = False):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _check_job(self, connection, job: IndexJob) -> None:
        row = connection.execute("SELECT * FROM index_jobs WHERE job_id=? AND token=? AND owner_id=? AND project_id=?",
                                 (job.job_id, job.token, self.scope.owner_id, self.scope.project_id)).fetchone()
        if not row or row["status"] != "running" or row["lease_expires"] <= self.clock():
            raise KnowledgeError("index_busy", "index writer lease was lost; the active snapshot is unchanged")

    def acquire_job(self) -> IndexJob:
        now = self.clock()
        with self.connect(write=True) as connection:
            row = connection.execute("SELECT * FROM index_jobs WHERE owner_id=? AND project_id=? AND status='running'",
                                     (self.scope.owner_id, self.scope.project_id)).fetchone()
            if row:
                if row["lease_expires"] > now:
                    raise KnowledgeError("index_busy", "another index writer currently owns this project")
                connection.execute("UPDATE index_jobs SET status='abandoned',error_category='lease_expired' WHERE job_id=?", (row["job_id"],))
                connection.execute("UPDATE snapshots SET state='failed',error_category='lease_expired' "
                                   "WHERE snapshot_id=? AND state IN ('building','ready') AND published_at IS NULL", (row["snapshot_id"],))
            job = IndexJob(uuid.uuid4().hex, uuid.uuid4().hex)
            connection.execute("INSERT INTO index_jobs VALUES(?,?,?,?,?,?,?,?)",
                               (job.job_id, self.scope.owner_id, self.scope.project_id, job.token, "running", None,
                                now + self.lease_seconds, None))
        return job

    def renew(self, job: IndexJob) -> None:
        with self.connect(write=True) as connection:
            self._check_job(connection, job)
            connection.execute("UPDATE index_jobs SET lease_expires=? WHERE job_id=?", (self.clock() + self.lease_seconds, job.job_id))

    def finish_job(self, job: IndexJob, *, error_category: str | None = None) -> None:
        with self.connect(write=True) as connection:
            connection.execute("UPDATE index_jobs SET status=?,error_category=? WHERE job_id=? AND token=? AND status='running'",
                               ("failed" if error_category else "complete", error_category, job.job_id, job.token))
            if error_category:
                connection.execute("UPDATE snapshots SET state='failed',error_category=? WHERE snapshot_id="
                                   "(SELECT snapshot_id FROM index_jobs WHERE job_id=? AND token=?) AND published_at IS NULL",
                                   (error_category, job.job_id, job.token))

    def active_id(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT snapshot_id FROM active_snapshots WHERE owner_id=? AND project_id=?",
                                     (self.scope.owner_id, self.scope.project_id)).fetchone()
            return row[0] if row else None

    def manifest(self, snapshot_id: str | None = None, *, published: bool = True) -> SnapshotManifest:
        snapshot_id = snapshot_id or self.active_id()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM snapshots WHERE snapshot_id=? AND owner_id=? AND project_id=?",
                                     (snapshot_id, self.scope.owner_id, self.scope.project_id)).fetchone()
        if not row or (published and (row["published_at"] is None or row["state"] not in {"published", "retired"})):
            raise KnowledgeError("index_missing", "the requested published snapshot is not available in this project")
        try:
            manifest = SnapshotManifest.from_dict(json.loads(row["manifest_json"]))
            if manifest.schema_version != SCHEMA_VERSION or manifest.owner_id != self.scope.owner_id or manifest.project_id != self.scope.project_id:
                raise ValueError("manifest scope mismatch")
            return manifest
        except (ValueError, TypeError, KeyError):
            raise KnowledgeError("index_corrupt", "snapshot manifest is invalid") from None

    def chunks(self, snapshot_id: str, chunk_ids: list[str] | None = None) -> list[Chunk]:
        self.manifest(snapshot_id)
        wanted = set(chunk_ids) if chunk_ids is not None else None
        with self.connect() as connection:
            rows = connection.execute("SELECT c.chunk_json FROM chunks c JOIN snapshot_chunks s ON c.chunk_id=s.chunk_id "
                                      "WHERE s.snapshot_id=? AND c.project_id=? ORDER BY c.chunk_id", (snapshot_id, self.scope.project_id)).fetchall()
        try:
            result = [Chunk.from_dict(json.loads(row[0])) for row in rows]
        except (ValueError, TypeError, KeyError):
            raise KnowledgeError("index_corrupt", "indexed chunk metadata is invalid") from None
        return [chunk for chunk in result if wanted is None or chunk.chunk_id in wanted]

    def cache(self, space: EmbeddingSpace) -> dict[str, tuple[float, ...]]:
        from .embedding_gateway import validate_vectors

        with self.connect() as connection:
            rows = connection.execute("SELECT text_hash,vector_json FROM embedding_cache WHERE owner_id=? AND project_id=? AND space_id=?",
                                      (self.scope.owner_id, self.scope.project_id, space.space_id)).fetchall()
        try:
            return {row[0]: validate_vectors([json.loads(row[1])], count=1, dimensions=space.dimensions)[0] for row in rows}
        except (ValueError, TypeError):
            raise KnowledgeError("index_corrupt", "embedding cache is invalid") from None

    def put_cache(self, job: IndexJob, space: EmbeddingSpace, vectors: dict[str, tuple[float, ...]]) -> None:
        from .embedding_gateway import validate_vectors

        with self.connect(write=True) as connection:
            self._check_job(connection, job)
            for key, vector in vectors.items():
                validate_vectors([vector], count=1, dimensions=space.dimensions)
                connection.execute("INSERT OR REPLACE INTO embedding_cache VALUES(?,?,?,?,?)",
                                   (self.scope.owner_id, self.scope.project_id, space.space_id, key, json.dumps(vector)))

    def stage(self, job: IndexJob, manifest: SnapshotManifest, chunks: list[Chunk]) -> None:
        if manifest.owner_id != self.scope.owner_id or manifest.project_id != self.scope.project_id:
            raise KnowledgeError("scope_mismatch", "snapshot does not belong to this index writer")
        if set(manifest.chunk_ids) != {chunk.chunk_id for chunk in chunks} or len(manifest.chunk_ids) != len(chunks):
            raise KnowledgeError("index_corrupt", "snapshot chunk inventory is inconsistent")
        with self.connect(write=True) as connection:
            self._check_job(connection, job)
            connection.execute("UPDATE index_jobs SET snapshot_id=? WHERE job_id=?", (manifest.snapshot_id, job.job_id))
            existing = connection.execute("SELECT published_at,state FROM snapshots WHERE snapshot_id=?", (manifest.snapshot_id,)).fetchone()
            if existing and existing[0] is not None and existing[1] in {"published", "retired"}:
                return
            connection.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET "
                               "state='building',manifest_json=excluded.manifest_json,published_at=NULL,error_category=NULL",
                               (manifest.snapshot_id, self.scope.owner_id, self.scope.project_id, "building",
                                json.dumps(manifest.to_dict()), self.clock(), None, None))
            connection.execute("DELETE FROM source_files WHERE snapshot_id=?", (manifest.snapshot_id,))
            connection.execute("DELETE FROM snapshot_chunks WHERE snapshot_id=?", (manifest.snapshot_id,))
            for source in manifest.files:
                connection.execute("INSERT INTO source_files VALUES(?,?,?,?,?)", (manifest.snapshot_id, source["source_path"],
                                   source["raw_hash"], source["normalized_hash"], json.dumps(source)))
            for chunk in chunks:
                if chunk.project_id != self.scope.project_id:
                    raise KnowledgeError("scope_mismatch", "chunk does not belong to this project")
                connection.execute("INSERT OR IGNORE INTO chunks VALUES(?,?,?)", (chunk.chunk_id, chunk.project_id, json.dumps(chunk.to_dict())))
                connection.execute("INSERT INTO snapshot_chunks VALUES(?,?)", (manifest.snapshot_id, chunk.chunk_id))

    def ready(self, job: IndexJob, snapshot_id: str) -> None:
        with self.connect(write=True) as connection:
            self._check_job(connection, job)
            connection.execute("UPDATE snapshots SET state='ready' WHERE snapshot_id=? AND state='building'", (snapshot_id,))

    def publish(self, job: IndexJob, snapshot_id: str) -> None:
        with self.connect(write=True) as connection:
            self._check_job(connection, job)
            row = connection.execute("SELECT * FROM snapshots WHERE snapshot_id=? AND owner_id=? AND project_id=?",
                                     (snapshot_id, self.scope.owner_id, self.scope.project_id)).fetchone()
            if not row or row["state"] not in {"ready", "published", "retired"}:
                raise KnowledgeError("index_corrupt", "only a complete snapshot may be published")
            manifest = SnapshotManifest.from_dict(json.loads(row["manifest_json"]))
            count = connection.execute("SELECT count(*) FROM snapshot_chunks WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0]
            fts_count = connection.execute("SELECT count(*) FROM chunk_fts WHERE snapshot_id=? AND owner_id=? AND project_id=?",
                                          (snapshot_id, self.scope.owner_id, self.scope.project_id)).fetchone()[0]
            if count != len(manifest.chunk_ids) or fts_count != count:
                raise KnowledgeError("index_corrupt", "snapshot storage counts are incomplete")
            connection.execute("UPDATE snapshots SET state='retired' WHERE snapshot_id IN (SELECT snapshot_id FROM active_snapshots "
                               "WHERE owner_id=? AND project_id=?)", (self.scope.owner_id, self.scope.project_id))
            connection.execute("UPDATE snapshots SET state='published',published_at=COALESCE(published_at,?) WHERE snapshot_id=?",
                               (self.clock(), snapshot_id))
            connection.execute("INSERT INTO active_snapshots VALUES(?,?,?) ON CONFLICT(owner_id,project_id) "
                               "DO UPDATE SET snapshot_id=excluded.snapshot_id", (self.scope.owner_id, self.scope.project_id, snapshot_id))

    @contextmanager
    def pin(self, snapshot_id: str | None = None):
        token = uuid.uuid4().hex
        with self.connect(write=True) as connection:
            if snapshot_id is None:
                row = connection.execute("SELECT snapshot_id FROM active_snapshots WHERE owner_id=? AND project_id=?",
                                         (self.scope.owner_id, self.scope.project_id)).fetchone()
                snapshot_id = row[0] if row else None
            row = connection.execute("SELECT state,published_at FROM snapshots WHERE snapshot_id=? AND owner_id=? AND project_id=?",
                                     (snapshot_id, self.scope.owner_id, self.scope.project_id)).fetchone()
            if not row or row[1] is None or row[0] not in {"published", "retired"}:
                raise KnowledgeError("index_missing", "no published snapshot is available")
            connection.execute("INSERT INTO snapshot_readers VALUES(?,?,?)", (token, snapshot_id, self.clock() + 300))
        try:
            yield self.manifest(snapshot_id)
        finally:
            with self.connect(write=True) as connection:
                connection.execute("DELETE FROM snapshot_readers WHERE token=?", (token,))

    def status(self) -> dict:
        active = self.active_id()
        if not active:
            return {"indexed": False, "snapshot_id": None}
        manifest = self.manifest(active)
        return {"indexed": True, "snapshot_id": active, "files": len(manifest.files), "chunks": len(manifest.chunk_ids),
                "embedding_space": manifest.to_dict()["embedding_space"], "splitter_version": manifest.splitter_version}

    def claim_cleanup(self, job: IndexJob, *, keep_retired: int = 1, grace_seconds: float = 3600) -> list[SnapshotManifest]:
        if keep_retired < 0 or grace_seconds < 0:
            raise ValueError("snapshot retention limits cannot be negative")
        with self.connect(write=True) as connection:
            self._check_job(connection, job)
            connection.execute("DELETE FROM snapshot_readers WHERE expires_at<=?", (self.clock(),))
            rows = connection.execute(
                "SELECT * FROM snapshots WHERE owner_id=? AND project_id=? AND snapshot_id NOT IN "
                "(SELECT snapshot_id FROM active_snapshots) AND snapshot_id NOT IN (SELECT snapshot_id FROM snapshot_readers) "
                "ORDER BY published_at DESC,created_at DESC",
                (self.scope.owner_id, self.scope.project_id),
            ).fetchall()
            manifests, retired_kept = [], 0
            for row in rows:
                if row["state"] == "retired":
                    retired_kept += 1
                    if retired_kept <= keep_retired or row["published_at"] > self.clock() - grace_seconds:
                        continue
                elif row["state"] != "failed":
                    continue
                # Make a retired snapshot unavailable before external deletion.
                # If Chroma deletion fails, gc_pending remains replayable and
                # no reader can mistake its partial vectors for a complete index.
                connection.execute("UPDATE snapshots SET state='failed',error_category='gc_pending' WHERE snapshot_id=?", (row["snapshot_id"],))
                manifests.append(SnapshotManifest.from_dict(json.loads(row["manifest_json"])))
        return manifests

    def finish_cleanup(self, job: IndexJob, snapshot_id: str) -> None:
        with self.connect(write=True) as connection:
            self._check_job(connection, job)
            row = connection.execute("SELECT state,error_category FROM snapshots WHERE snapshot_id=? AND owner_id=? AND project_id=?",
                                     (snapshot_id, self.scope.owner_id, self.scope.project_id)).fetchone()
            if not row:
                return
            if row[0] != "failed" or row[1] != "gc_pending":
                raise KnowledgeError("index_busy", "only a claimed inactive snapshot may be removed")
            if connection.execute("SELECT 1 FROM active_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone():
                raise KnowledgeError("index_busy", "the active snapshot cannot be removed")
            connection.execute("DELETE FROM chunk_fts WHERE snapshot_id=? AND owner_id=? AND project_id=?",
                               (snapshot_id, self.scope.owner_id, self.scope.project_id))
            connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM chunks WHERE project_id=? AND chunk_id NOT IN (SELECT chunk_id FROM snapshot_chunks)",
                               (self.scope.project_id,))
