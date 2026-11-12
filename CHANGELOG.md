# Changelog

This changelog covers changes made by the reliability fork. Upstream CoreCoder history remains in Git and is not relabeled as fork work.

## Unreleased — Reliability preview

### Added by this fork

- Explicit `RunState`, structured `AgentResult`, and backward-compatible `Agent.run()`/`Agent.chat()` boundary.
- Unified run budgets, cooperative cancellation, action fingerprints, and no-progress termination.
- `ToolSpec`, `ToolResult`, controlled errors, legacy adapters, and metadata-driven safe scheduling.
- Default-off redacted Trace sinks plus offline replay.
- Evidence-producing Verifiers that prevent model prose from directly marking success.
- Transactional SQLite task journal, idempotency keys, conservative recovery, and CLI resume/status commands.
- Deterministic 40-case Eval Harness, fault injection, matched comparison, and CI safety gates.
- Reproducible baseline documentation, progressive learning material, architecture notes, ADRs, and a real crash/resume offline demo.
- README benchmark generation and validation from checked-in JSON evidence.

### Compatibility

- `Agent.chat()` still returns `str`; unverified results are now explicit at the display boundary.
- Existing tools may still return strings through `LegacyToolAdapter`.
- File-level checkpoint `/undo` remains separate from task-level persistence.

## Upstream baseline — CoreCoder 0.8.0

- Source: `https://github.com/he-yufeng/CoreCoder`
- Commit: `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0`
- Attribution: inherited, not authored by this fork.

The fork has not created or published a new package release yet.
