# 本地演示

在项目根运行，或先安装 wheel 后使用对应示例源码。Python 3.11+：

```powershell
python -m pip install -e ".[knowledge,graph,mcp]"
```

以下脚本使用临时目录、真实存储和实际框架。生成与向量是固定替身，不需要 Key，不访问模型、不收费；不评估真实语义质量。每个断言失败都会非零退出，不预填“通过”。

完整链路可直接运行：

```powershell
python -m examples.full_stack_demo
python -m evals.full_stack_runner
```

第一条把 OpenAI SDK 请求与流式响应解析、版本化 Prompt、LangChain、RAG、长期记忆、LangGraph 和 MCP 串成同一流程。SDK 使用进程内 HTTP 替身，未访问真实模型；存储、框架和 MCP stdio 实际运行。演示检查候选记忆审核、跨会话召回、偏好改写与删除、引用修复、源码更新、终态恢复及三种入口来源一致性。

第二条汇总完整链路、Prompt v1/v2 契约、Agent MCP 调用和真实文件写入后的崩溃恢复，任一失败则非零退出。报告默认写入 `evals/results/full-stack/acceptance.json`。技术栈对应实现和真实 API 验收见 [完整链路说明](full-stack.md)。

| 演示 | 命令 | 实际断言 |
|---|---|---|
| 仓库问答 | `python -m examples.repo_qa_demo` | 能找到源码、引用属于当前 snapshot；无答案返回 insufficient；编辑后刷新并使用新引用 |
| 长期记忆 | `python -m examples.memory_demo` | 跨服务实例召回；改写取代旧偏好；删除后排除，旧操作不能复活 |
| MCP 与图 | `python -m examples.knowledge_mcp_demo` | 图内服务、Agent 的 MCP 工具、独立官方客户端返回同一来源版本；桥接线程关闭 |
| 同一演示的恢复部分 | `python -m examples.graph_resume_demo` | 文件写入后、收据前中断；恢复同一 child run；物理写入一次；终态恢复不增加模型请求 |

更底层的 `python -m examples.knowledge_index_demo` 检查冷建、零变化、单文件更新、改名、故障和增量缓存。独立客户端也可查询已有项目索引：

```powershell
python -m examples.knowledge_mcp_client --root /absolute/project --query RunStore
```

它默认只用词法搜索，不读取 Chat Key。建索引是单独的可信主机操作，MCP 不提供索引或写入接口。SDK 示例配置在 `examples/knowledge-mcp.json`，必须替换绝对路径和 Python 执行文件。

若提示缺失扩展，检查所用 Python 是否就是安装依赖的解释器；Chroma 的原生依赖也必须安装完整。`index_missing` 表示目标项目尚未建库；`stale_evidence` 表示文件与索引不同，应重新索引。权限、暂停和 UNKNOWN 恢复不应通过重建新任务绕过。

原运行时/图配对恢复检查：

```powershell
python -m evals.recovery_comparison
python -m evals.workflow_runner
```

前者比较相同 WriteFileTool、固定模型、文件验证器和三个故障边界，两组都接入持久预算；后者执行冻结的 12 个图恢复测试。它们不代表任意外部系统都能恰好一次执行，也不证明模型能力或耗时提升。
