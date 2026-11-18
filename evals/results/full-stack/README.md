# 完整链路验收记录

2026-11-18，Windows / Python 3.12.16。

| 检查 | 结果 | 证据 |
|---|---|---|
| SDK、Prompt、记忆、RAG、LangGraph、MCP 统一演示 | 11 项检查通过，三轮问答 | `acceptance.json` → `stages.full_stack` |
| Prompt v1/v2 契约 | 各 14/14 | `acceptance.json` → `stages.prompt_contracts` |
| Agent MCP 来源 | 与独立客户端、图内问答一致 | `acceptance.json` → `stages.agent_mcp_sources` |
| 编码任务崩溃恢复 | 物理写入一次，终态恢复新增请求为 0 | `acceptance.json` → `stages.coding_crash_resume` |
| 全量离线回归 | 579 passed、1 skipped | `regression.json`、`regression.xml` |
| 真实 Chat API | 通过，1 次请求、610 Token | `live-api-summary.json` |
| 真实 Embedding API | 缺少独立 Key，未验收 | `live-api-summary.json` |

离线验收使用固定模型/向量替身；SDK、存储、框架、MCP 传输与文件写入实际运行。全量回归排除 `tests/test_live_smoke.py`，在正常进程终止权限下执行，跳过项为 Windows 文件 symlink 权限限制。

真实 API 探针只发送固定示例句子与证据，验收连接和结构化输出，费用未配置可信费率时保持未知。Embedding 未配置，因此真实探针整体为 `incomplete`。这些结果不代表真实语料 Recall、答案支持率、Prompt 优劣或记忆遵守率已达标。

复现命令和实现对应关系见 [完整链路说明](../../../docs/full-stack.md)。
