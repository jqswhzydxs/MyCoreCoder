# 本地知识 MCP

`corecoder mcp serve-knowledge` 用官方 `mcp==2.3.0` SDK 的 `MCPServer` 包装已有 KnowledgeService。
它只提供 `knowledge_search` 和 `knowledge_get_chunk`，不提供建索引、写文件或改变项目根的工具。
可信主机启动时指定项目根，客户端只能传问题、topK 和已生成的 snapshot/chunk ID。

安装与使用：

```powershell
python -m pip install -e ".[knowledge,mcp]"
python -m corecoder knowledge index --root . --embedding-provider fixture
python -m examples.knowledge_mcp_client --root . --query RunStore
```

上面的固定向量只用于离线演示；客户端默认 lexical，不请求 Chat 或 Embedding。
真实向量模式使用独立 Embedding 配置，不能用固定哈希向量验证语义质量。客户端执行文件及服务器必须使用安装了相同扩展的环境。

## 服务边界

- 每次查询重新检查 scope、发布版本和源码哈希，旧文件内容不会以当前有效来源返回。
- 搜索最多返回 10 个结果，每个摘要 500 字符；读取正文最多 12,000 字符，响应 JSON 最多 64 KiB。
- 参数 Schema 和中间件拒绝未知字段，不能通过工具参数覆盖 root 或运行配置。
- stdout 只由 SDK 输出协议；配置错误与诊断使用 stderr。服务错误不返回 provider traceback 或原始异常正文。
- 每次服务请求关闭向量句柄。查询 Embedding 共用服务器进程自己的预算，默认 200 次请求、200 万估算 Token、1 小时；可用对应 CLI 参数收紧。
- `server_budget` 明确返回服务器账本。它与 Agent 所在进程的模型账本分别计数，首版没有跨进程统一预算服务。词法查询不消耗模型请求。

## SDK 桥接

`MCPToolBridge` 在独立 asyncio 线程中的一个长期任务内进入和退出官方 Client 上下文。同步 Tool 调用可以来自已有 asyncio 循环；不嵌套 `asyncio.run()`。
发现工具时遍历分页，限制 32 页/512 工具并拒绝重复名称和循环 cursor。输入和输出 Schema 用于 ToolSpec，重连拒绝已知工具契约变化。
超时和调用方取消会取消 SDK 请求；关闭桥接等待线程结束并由 SDK 回收 stdio 子进程。断线不自动重放调用。

远端 `readOnlyHint` 不授予本地权限。默认工具按外部副作用、需要确认处理；只有可信本地配置的 `trustedReadOnlyTools` 才标记为只读且可幂等执行。来源校验要求每次调用读取当前数据，因此不使用工具结果缓存。
SDK 错误转换为不含远端正文的 ToolError；原始服务器 stderr 不转发给模型。

SDK 的 stdio 子进程默认只继承最小环境。词法模式无需密钥；独立客户端显式选择 vector/hybrid 时只转发知识/Embedding 配置变量和指定的 Embedding Key，不转发完整父进程环境。通用 MCPToolBridge 的可信调用者需自行提供 `SDKServerConfig.env`，SDK 不会自动传递其他服务的密钥；避免把密钥写进受版本控制的 MCP JSON。

示例配置在 `examples/knowledge-mcp.json`，使用前替换项目绝对路径及 Python 执行文件。`client: sdk` 才选择新桥接；未配置时继续使用旧 MCPClient。原客户端与新 SDK 的 2025 兼容模式均有真实 stdio 用例；`auto` 在受测 SDK 中协商到 2026-07-28。
这不包含远程 HTTP、鉴权或多用户部署。

## 可重复验证

```powershell
python -m pip install -e ".[knowledge,graph,mcp]"
python -m pytest tests/integrations/test_knowledge_mcp.py -q
python -m examples.knowledge_mcp_demo
python -m examples.graph_resume_demo
```

MCP 演示在临时项目中真实建索引、运行 StateGraph、启动两个本地 stdio 服务；断言图内问答、Agent 工具与独立 SDK 客户端返回相同 snapshot/chunk。
图恢复演示另行注入真实文件写入后的中断，并断言复用原 child run、物理写入恰好一次、终态恢复不增加请求。
生成和向量均为固定替身，不联网、不收费；断言失败返回非零。MCP 测试还覆盖分页、非法参数、源码变更、超时、取消、断线、重连与子进程回收。
设置 `CORECODER_REQUIRE_MCP_TESTS=1` 时依赖缺失直接失败。

API 依据为安装版本的官方 SDK 代码；官方说明参见 [Client](https://py.sdk.modelcontextprotocol.io/client/)、[协议兼容](https://py.sdk.modelcontextprotocol.io/protocol-versions/) 和 [分页](https://py.sdk.modelcontextprotocol.io/advanced/pagination/)。
