# CoreCoder Reliability Fork

An offline-first reliability fork of [he-yufeng/CoreCoder](https://github.com/he-yufeng/CoreCoder), based on upstream 0.8.0 at commit `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0`.

[中文文档](README_CN.md) · [Execution plan](docs/roadmap/llm-engineering-execution-plan.zh-CN.md) · [Implementation status](docs/roadmap/implementation-status.zh-CN.md)

Inherited work: the original coding loop, tools, provider adapters, permissions, hooks, MCP client, CLI, sessions, and educational articles.

Added by this fork: explicit run state, scheduling, budgets, evidence verification, durable recovery, steering, offline evaluations, repository knowledge, scoped memory, graph workflows and an official-SDK knowledge MCP bridge. Upstream authorship and the MIT license are retained.

Run `python -m examples.full_stack_demo` for the integrated SDK/Prompt/LangChain/RAG/memory/LangGraph/MCP demo, or `python -m evals.full_stack_runner` for unified acceptance. Models and vectors are offline fixtures by default; the bounded live API probe is opt-in. See [the full-stack guide](docs/full-stack.md).

## Install and run

```bash
python -m pip install -e ".[dev]"
python -m examples.reliablecoder_demo
corecoder --demo
```

The default demos and tests use scripted models and require no credentials. For a live coding session, configure your provider through the environment as described in [the Chinese guide](README_CN.md). `Agent.chat()` continues to return a string, and only trusted Verifiers can mark coding runs `VERIFIED`.

## Optional Prompt chains

```bash
python -m pip install -e ".[knowledge]"
python -m examples.prompt_gateway_demo
python -m evals.prompt_runner --compare v1 v2 --offline
```

This milestone implements versioned repository-answer, task-plan, and memory-extraction prompts using an actual LangChain `ChatPromptTemplate → Runnable → Pydantic` chain. The extension requires Python 3.11+; base installation stays on Python 3.10+. A shared ledger counts every request, including retries and the single permitted schema repair. SDK retries are disabled for the new gateway path. Unknown usage and cost remain explicit; input token budgeting uses a conservative estimate. No proposal grants permissions or commits memory.

## Repository knowledge, memory and workflows

```bash
python -m pip install -e ".[knowledge,graph,mcp]"
python -m examples.repo_qa_demo
python -m examples.memory_demo
python -m examples.knowledge_mcp_demo
python -m examples.graph_resume_demo
```

These assertions use real SQLite/FTS5, Chroma, LangChain, StateGraph and MCP stdio with fixed embeddings and generation. They need no model keys and make no model requests. The knowledge loader indexes authorized Python/Markdown with exact source spans, immutable incremental snapshots, explicit embedding vectors and RRF retrieval. Ask validates evidence IDs and current source hashes. Memory has project scope, source checks, expiry and deletion tombstones. Graph execution keeps the existing Agent/Verifier in charge of code effects, with stable child identities and persistent budgets.

```bash
corecoder knowledge index --root . --dry-run
corecoder knowledge index --root . --mode lexical
corecoder knowledge search RunStore --root . --mode lexical
corecoder ask RunStore --root . --mode lexical --offline-fixture --sources
corecoder memory remember "Answer in Chinese" --root .
corecoder memory list --root .
corecoder mcp serve-knowledge --root . --mode lexical
```

Local keyword indexing and search need no Embedding service or API key. Set `[knowledge] retrieval_mode = "lexical"` in `corecoder.toml` to make both commands use it by default. Semantic retrieval is optional: configure a real Embedding model and rebuild with `--mode hybrid`. Live answer generation requires Chat configuration; `--offline-fixture` only demonstrates the answer pipeline. Hash vectors only exercise the pipeline. Graph code runs require an explicit trusted verification policy; checkpoints contain references rather than source/prompt bodies. MCP uses a root-bound read-only server and a closable dedicated SDK loop. Its process-local embedding budget is reported separately from the caller's ledger.

See [knowledge](docs/design/06-repository-knowledge.md), [memory](docs/design/08-project-memory.md), [graph](docs/design/09-durable-graph-workflows.md), [MCP](docs/design/10-knowledge-mcp.md) and the [current execution record](docs/roadmap/next-stage-status.zh-CN.md). Live semantic Recall and human answer-support targets remain unaccepted; the [quality protocol](docs/quality-evaluation.md) separates model experiments from fixtures. CI matrices are configured; local results do not claim a cloud CI run.

## Offline checks

```bash
python -m pytest tests --ignore=tests/test_live_smoke.py -q
python -m ruff check corecoder tests examples evals
python -m evals.readme_benchmark --check
python -m pip check
python -m build
python -m twine check dist/*
```

Install `build` and `twine` separately for packaging. See [CONTRIBUTING.md](CONTRIBUTING.md), [design documents](docs/design/), [CHANGELOG.md](CHANGELOG.md), and [RELEASE_NOTES.md](RELEASE_NOTES.md).

## Existing deterministic reliability evidence

These checked-in reports describe scripted offline cases, not live model quality or newly measured performance gains.

<!-- eval-benchmark:start -->
<!-- Generated by: python -m evals.readme_benchmark --write -->
| Metric | Baseline | Candidate |
|---|---:|---:|
| Task success rate | 80% | 90% |
| Verified completion rate | 5% | 5% |
| False completion rate | 11.1111% | 9.0909% |
| Duplicate side-effect rate | 28.5714% | 0% |
| Permission bypass rate | 0% | 0% |
| Crash resume rate | 0% | 100% |
| P95 rounds | 3 | 2 |
| Completion tokens | 180 | 188 |

Evidence: 40 deterministic offline cases, 1 run per case, model `FaultInjectingLLM/scripted-eval-v2`, commit `50e5e7df168d708b45ed0eb43d7fffaa9edff5d0`. Safety gates: **PASS**.
<!-- eval-benchmark:end -->

Current source size: `engine-4778_LoC`; **83 files**, **17,649 physical lines**, **15,510 net** lines.
