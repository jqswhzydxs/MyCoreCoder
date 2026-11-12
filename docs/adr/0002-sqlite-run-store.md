# ADR-0002: Persist RunState in SQLite

- Status: Accepted
- Date: 2026-11-04

## Context

Crash recovery needs transactional updates across a run, state versions, tool-call lifecycle, evidence, and Trace references. A JSON file is easy to inspect but cannot atomically coordinate these related records or enforce uniqueness of idempotency keys. A remote database would add deployment and network dependencies to an offline-first project.

## Decision

Use Python's standard-library `sqlite3` with transactions, WAL mode, foreign keys, schema versioning, uniqueness constraints, and a database per configured runtime. Persist sanitized snapshots at model, tool, and verification boundaries.

Keep inherited file checkpoints for `/undo`; SQLite journals task execution and references checkpoints/hashes when reconciling file side effects.

## Consequences

- The default recovery path remains local, offline, single-file, and reproducible.
- Atomic lifecycle changes and stable idempotency keys can be queried and tested.
- Corruption or unsupported schema versions can fail closed before replaying side effects.
- SQLite is not a distributed coordination service. Multi-host workers would need a different store implementation and stronger leases.
- Verifier objects and secrets are not serialized; callers must re-supply trusted configuration on resume.
