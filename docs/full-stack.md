# 完整技术链路与验收

本项目是开源 CoreCoder 的二次开发。此次补充统一演示和验收入口，复用已有模块，没有重新实现一套 Agent。

## 技术栈与实现

| 技术 | 已有实现 | 统一演示中的证据 |
|---|---|---|
| Python、LLM API | `llm.py`、`integrations/model_gateway.py` | 生产 LLM 适配器通过官方 OpenAI SDK 发送请求并解析 SSE；离线 HTTP 响应是替身 |
| Prompt Engineering | `prompts/`、`integrations/langchain_bridge.py` | 记录模板版本和 SHA-256；注入未知引用，验证一次有上限的结构修复 |
| LangChain | `StructuredPromptChain`、`knowledge/retriever.py` | 实际 ChatPromptTemplate/Runnable 链及 BaseRetriever |
| 长期记忆 | `memory/` | 模型提取先存 candidate，审核后跨实例召回；改写和删除影响下一次实际请求上下文 |
| RAG | `knowledge/` | 真实 SQLite FTS5、Chroma 和 RRF；行级引用；源码更新后发布新快照 |
| LangGraph | `workflows/` | prepare → retrieve → recall → plan → answer → finalize；SQLite 检查点恢复不重复调用模型 |
| MCP | `mcp_servers/knowledge.py`、`integrations/mcp_sdk_bridge.py` | 图内问答、同步桥接、独立 SDK 客户端返回相同源码版本；桥接关闭 |

记忆只作为历史参考，不能取代当前源码证据。示例核对的是传入模型的上下文，不把替身输出当作真实模型遵守偏好的证明。

## 离线验收

在源码根目录，使用已安装 `.[knowledge,graph,mcp]` 的 Python 3.11+：

```powershell
python -m examples.full_stack_demo
python -m evals.full_stack_runner
```

统一演示包括三次问答：审核后的偏好、改写后的偏好、删除偏好并更新源码。它检查实际 API 消息、当前源码哈希、图节点顺序、检查点恢复和 MCP 来源；不只检查模块能否导入。默认临时工作区自动清理，不修改当前项目的索引或记忆。

统一验收额外运行：

- Prompt v1/v2 的同响应契约对照；不是模型质量 A/B 实验。
- 原 Agent 真实调用 MCP 工具，核对来源。
- 文件已写但回执未存时注入崩溃，恢复同一子任务，核对只写一次及终态新增请求为零。

默认报告为 `evals/results/full-stack/acceptance.json`，包含状态、依赖、阶段结果及源码哈希。失败不会被汇总为通过。历史报告只对应所记录的源码。

## 真实模型 API 小样本

已有 API 适配不等于真实服务已验收。以下命令才读取 Chat/Embedding 凭据并调用配置的服务，可能计费：

```powershell
python -m evals.full_stack_runner --live-api
```

它在离线验收后追加独立探针：用 `repo_answer.v2` 对单条固定证据生成结构化回答，再对一条固定句子请求 Embedding。总上限为 4 次请求、30,000 Token 预算、60 秒；网关和 SDK 均关闭自动重试，结构修复最多一次。仅发送示例文本，不发送工作仓库、历史记忆或密钥正文。

Chat 使用原有配置解析，Embedding 使用独立模型、地址和 Key 环境变量。两者分别记录；缺少配置记为 `not_configured`，总体验收为 `incomplete` 且非零退出，不回退到替身。输出只保留模型名、维度、Schema 结果与用量账本，不保存回答正文、地址或凭据。真实探针报告默认留在忽略目录 `.corecoder/acceptance/live.json`。

这个探针只验收连接、用量记录和输出契约。真实语料 Recall、答案语义支持、记忆遵守率、Prompt 优劣和人工复核仍按 [质量实验协议](quality-evaluation.md) 单独验收，不用离线通过数代替。
