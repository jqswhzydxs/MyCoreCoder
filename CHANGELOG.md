# Changelog

This changelog covers changes made by the reliability fork. Upstream CoreCoder history remains in Git and is not relabeled as fork work.

## Unreleased — Reliability preview

### Repository knowledge and durable workflows (2026-11-17)

- Add authorized Python/Markdown loading, AST/heading line-preserving splitting, independent budgeted Embedding, immutable manifests, explicit Chroma vectors, FTS5 and hybrid retrieval.
- Add cold `knowledge index --mode lexical` with no Embedding/key/model calls or Chroma; share the configured default mode with search, refresh lexical evidence locally and preserve separate vector caches when switching modes.
- Add evidence-checked answers, source freshness/refusal behavior, local knowledge/ask CLI and a frozen 40-question development/holdout corpus.
- Add scoped SQLite memory, trusted provenance, candidate approval, expiry/conflicts and deletion tombstones; integrate recall without granting permissions.
- Add actual LangGraph stages, JSON checkpoints with controlled payload references, stable Agent run identities, durable budgets and conservative child recovery.
- Add root-bound read-only MCP tools using official SDK 2.3.0, a dedicated closable client loop, explicit SDK configuration and actual stdio/fault tests alongside legacy compatibility.
- Add offline demonstrations, matched runtime/graph recovery, opt-in bounded quality experiments and separate human-review gates. Real semantic quality and human support targets remain pending; this is not a published release.

### Model and Prompt integration (2026-11-13)

- Repair missing package README and release documentation; ignore local `.corecoder/` runtime data.
- Add optional LangChain Prompt chains with packaged v1/v2 templates, SHA-256 provenance, bounded Pydantic proposals, evidence membership validation, and at most one schema repair.
- Add single-attempt `LLM.chat_once()` and ModelGateway with explicit error categories, shared request/token/deadline/cost accounting, bounded retries, complete tool-message pairing, and unknown usage/cost reporting.
- Keep existing `LLM.chat()` and Agent contracts compatible; hide API keys in Config representations and separate Embedding configuration from Chat configuration.
- Add deterministic Prompt comparisons, an offline gateway demo, and focused integration tests. Subsequent repository/memory/graph/MCP implementation is recorded separately above.

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
