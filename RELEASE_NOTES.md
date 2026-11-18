# Unreleased reliability and repository-workflow preview

This fork has not published a new package release. The package version remains the inherited CoreCoder 0.8.0 version.

The reliability layer adds structured execution, evidence-based verification, budgets, steering, conservative SQLite recovery, and offline evaluation. The optional `knowledge` extra adds versioned Prompt resources, strict proposals, LangChain composition, authorized repository indexing, Chroma/FTS5 retrieval, current-source answers and scoped project memory. The `graph` and `mcp` extras add actual StateGraph orchestration and an official-SDK read-only knowledge server/client bridge.

`Agent.chat()`, the existing coding runtime, and lightweight Python 3.10+ installation remain compatible. Extensions require Python 3.11+. The gateway uses `chat_once()` and owns retries; legacy `chat()` behavior is retained. Templates ship inside the wheel. Install `[knowledge,graph,mcp]` for all repository workflows. Code completion remains a Verifier decision; graph plans and memories cannot grant authority.

Local demonstrations use real storage/framework/stdio execution with fixed generation and vectors. They establish pipeline and recovery behavior. Real semantic Recall, human support, paid-provider cost and performance improvements remain unaccepted. MCP server budget accounting is process-local and separate from the caller. See [the current implementation status](docs/roadmap/next-stage-status.zh-CN.md), [demonstrations](docs/demos.md) and [quality protocol](docs/quality-evaluation.md) for evidence and limits.

[Upstream CoreCoder](https://github.com/he-yufeng/CoreCoder) at `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0`. Inherited code remains attributed to its authors under MIT; this fork adds the reliability and repository-workflow layers.
