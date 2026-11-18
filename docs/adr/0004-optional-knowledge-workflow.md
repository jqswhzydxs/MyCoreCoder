# ADR-0004: Optional knowledge workflows around the existing runtime

- Status: Accepted; knowledge, memory, graph and MCP adapters implemented, final quality acceptance pending
- Date: 2026-11-13

## Context

The engineering execution plan calls for evidence-backed repository answers, scoped long-term memory, and resumable high-level workflows. ADR-0001 keeps the explicit coding loop because it owns permissions, scheduling, evidence verification, and recovery. Replacing it would duplicate execution and authority.

## Decision

Introduce optional integrations in dependency order. The first implementation is a LangChain Prompt -> single-attempt ModelGateway -> strict Pydantic proposal chain. The concrete need is reusable, versioned task templates with validated structured results and measurable repair behavior. Framework imports occur only when the integration is used. Base installation remains Python 3.10+; extension compatibility is recorded separately from base compatibility.

ModelGateway owns a bounded retry loop. A new `LLM.chat_once()` disables SDK retries, buffers an entire stream, rejects malformed tool calls, and closes streams. The existing Agent continues using its compatible `LLM.chat()` path. One BudgetLedger spans all calls and schema repairs in a workflow. Unknown usage is charged conservatively and unknown cost is reported as unavailable. Trusted explicit rates are required to impose a USD budget. Neither LangChain nor model output may execute tools, set trusted identities, select verification commands, or write active memories.

KnowledgeService owns immutable source snapshots and retrieval, MemoryService owns scoped records and deletion tombstones, and LangGraph owns high-level stages. RuntimeBridge persists a stable child mapping before calling Agent.run_once and reads RunStore before replay. RunStore and Verifier remain execution and success authorities. Checkpoints contain references; controlled local payloads hold bodies. The MCP server wraps KnowledgeService using the pinned official SDK; the synchronous Tool bridge owns a dedicated asyncio task/thread and closes its stdio process. Existing MCPClient remains the default compatibility path. Implementation and measured limits are recorded in design/06, design/08, design/09, design/10 and the next-stage status report. Live-quality acceptance remains pending.

## Consequences

- ADR-0001 remains valid; there is only one coding tool execution loop.
- The optional `knowledge` dependency group enables indexed repository knowledge and Prompt integration. Graph and MCP remain separate extras.
- No top-level LangChain Agent, automatic telemetry or default live calls are introduced. StateGraph/SQLite and official SDK stdio have actual integration tests.
- Proposal schema and evidence membership checks prove structural validity, not semantic support. KnowledgeService separately validates current hashes and line spans.
- Offline comparisons prove pipeline behavior only. Tested dependency/platform combinations are recorded separately; actual model quality and live provider support require their own evidence. MCP query embeddings consume a separately reported server ledger, with no claim of cross-process unified accounting.

## References

- [LangChain ChatPromptTemplate](https://reference.langchain.com/python/langchain-core/prompts/chat/ChatPromptTemplate)
- [LangChain RunnableLambda](https://reference.langchain.com/python/langchain-core/runnables/base/RunnableLambda)
- [LangChain OpenAI message conversion](https://reference.langchain.com/python/langchain-core/messages/utils/convert_to_openai_messages)
- [LiteLLM retry ownership](https://docs.litellm.ai/docs/completion/reliable_completions)
