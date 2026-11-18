# 后续阶段实施状态

本轮起点：2026-11-16（Asia/Singapore）。当前工作延续到 2026-11-17。目标仍为[下一阶段执行计划](next-stage-execution-plan.zh-CN.md)的 S0–S6，不将已完成的基础子集替代整体目标。

## 起点证据

- [起始工作树与环境清单](../../evals/results/next-stage/starting-worktree.json)：HEAD、已有改动、关键源码和模板 SHA-256、依赖版本、SQLite/FTS5 能力。
- [起点检查结果](../../evals/results/next-stage/starting-checks.json)：436 项离线回归通过，Ruff、README benchmark、pip check 通过，原 Prompt v1/v2 各 14/14。
- 本轮未读取真实模型凭据或调用真实 Chat/Embedding API。

## 任务进度

| 编号 | 当前实现 | 证据与尚未完成的验收 |
|---|---|---|
| A01 | 已记录起点 | 上述 JSON；全量检查使用正常 Windows 进程终止权限 |
| A02 | 知识、图、MCP 三环境已验证 | Windows Python 3.11.18/3.12.16/3.14.8 的实际安装与对应联测；分阶段依赖快照在 requirements，非跨平台锁 |
| A03 | 领域契约已实现 | knowledge/models.py、memory/models.py、workflows/state.py；实现边界见 design/06、08、09 |
| A04 | 本地路由已实现 | knowledge/ask/memory 在旧 Chat Key 检查前分流；本地管理不读取 Chat 配置，旧参数保持兼容 |
| B01 | 已实现并测试 | 忽略规则、嵌套 include、UTF-8/CRLF、稳定读取、大小/二进制、越界与 Windows junction；文件 symlink 测试因账户权限有一项 skip |
| B02 | 已实现并测试 | AST/Markdown/LangChain 字符切分、decorator/嵌套符号、围栏、长块覆盖、精确回读 |
| B03 | 已实现并测试离线协议 | 独立兼容 API 适配、固定向量、重排/维度/有限数、重试/预算/未知 usage；真实后端尚未调用 |
| B04 | 已实现并联测 | schema、事务回滚、跨连接认领、租约和根绑定；六处索引故障注入保持已发布索引可读 |
| B05–B08 | 已通过本地存储联测与演示 | 真实 Chroma/FTS5、增量/改名缓存、原子发布、读者保护清理、CLI；不变索引新增 Embedding 请求为 0 |
| C01–C04 | 已通过离线端到端联测 | 真实 BaseRetriever/RRF、当前源码引用、一次刷新/改写、ask 人类/JSON 输出；生成使用固定替身，真实语义问答仍待验证 |
| C05 | 固定评测已执行 | 40 题，24 开发/16 保留，三模式报告见下；哈希向量召回较低，未达到或宣称真实语义质量门槛 |
| D01–D05 | 生命周期已实现并验证 | SQLite、显式偏好、候选审核、真实 VERIFIED 运行证据、TTL/冲突/来源、无正文删除 tombstone；20 组序列及问答上下文集成已测试 |
| E01–E07 | 已实现并通过离线验证 | 真实 StateGraph、JSON-only checkpoint、公开 Agent.run_once、进程认领、RuntimeBridge、持久总账、Agent/summary 预算、CLI；12 组恢复契约与真实写入演示已通过，综合消融另行完成 |
| F01–F05 | 已实现并真实 stdio 联测 | 官方 mcp 2.3.0 Server、专用 asyncio 线程 Tool 桥接、独立 SDK 客户端、旧客户端兼容；6 个实际传输测试覆盖分页/取消/超时/重连/参数/项目范围/服务预算/进程回收 |
| G01 | 离线对照与真实评测入口已实现 | 三路检索、记忆 off/on、Prompt v1/v2、原 runtime/图配对恢复；quality_runner 显式 live、同检索 Prompt 对照与人工标注绑定 |
| G02 | 受测硬门禁通过，真实质量暂缓 | 用户 2026-11-17 明确要求 Embedding 配置先留空，待申请服务；不声明真实 Recall 或人工支持率达标 |
| G03 | Windows 安装与包验证已执行 | 基础 3.10、扩展 3.11/3.12/3.14；wheel 在源码目录外实际运行问答、记忆、MCP/图演示；云端矩阵尚未运行 |
| G04 | 项目材料与三套演示已补齐 | README 中英文、CHANGELOG、RELEASE_NOTES、design/10、demos、quality-evaluation；上游 MIT 归属保留，外层简历不填未验收的成绩 |

## 当前局部检查

以下包含历史阶段证据；本轮收尾结果另列在后，不将旧检查视为所有后续源码的验证：

| 组合 | 结果 | 证据 |
|---|---|---|
| Windows/Python 3.12 知识扩展全量离线回归 | 524 passed、1 skipped，72.56 秒 | [full-py312.json](../../evals/results/next-stage/full-py312.json) |
| Windows/Python 3.10 基础安装回归 | 453 passed、35 skipped，56.75 秒；可选扩展按能力跳过 | [base-py310.json](../../evals/results/next-stage/base-py310.json) |
| Windows/Python 3.14 知识/Prompt/记忆联测 | 119 passed、1 skipped，23.07 秒 | [knowledge-py314.json](../../evals/results/next-stage/knowledge-py314.json) |
| Windows/Python 3.11 知识/Prompt/记忆联测 | 当时 117 passed、1 skipped，26.02 秒 | requirements 中保存该环境的 105 包依赖快照；后续增量测试另行记录 |
| 记忆序列 memory off/on | on 21/21 上下文检查，删除残留 0、项目越界 0 | [memory-fixture.md](../../evals/results/next-stage/memory-fixture.md)；不代表真实模型遵守率 |
| Windows/Python 3.12 图扩展全量离线回归 | 551 passed、1 skipped，83.48 秒 | [full-graph-py312.json](../../evals/results/next-stage/full-graph-py312.json)；随后新增输入新鲜度/共享预算用例另行通过 |
| Windows/Python 3.10 图基础兼容回归 | 461 passed、37 skipped，58.48 秒 | [base-graph-foundation-py310.json](../../evals/results/next-stage/base-graph-foundation-py310.json)；可选模块按能力跳过 |
| Windows/Python 3.11 图/知识/Prompt/记忆联测 | 149 passed、1 skipped，45.14 秒 | [graph-py311.json](../../evals/results/next-stage/graph-py311.json) |
| Windows/Python 3.14 图/知识/Prompt/记忆联测 | 149 passed、1 skipped，40.93 秒 | [graph-py314.json](../../evals/results/next-stage/graph-py314.json) |
| 固定图恢复契约 | 12/12 通过 | [workflow-contracts.md](../../evals/results/next-stage/workflow-contracts.md)；不是性能收益或原 runtime/图消融 |

唯一文件 symlink skip 来自 Windows 账户权限；Windows junction 越界用例实际通过。扩展测试显式要求依赖，Chroma 用例没有整组 skip。

初始检索报告：[开发集](../../evals/results/next-stage/knowledge-fixture-dev.md)、[保留集](../../evals/results/next-stage/knowledge-fixture-holdout.md)。三路检索返回的引用回读均有效，对抗检查均通过；固定哈希向量不是语义 Embedding，因此向量及混合召回数字不作为真实 Recall@5 ≥0.80 的验收。保留集未用于逐题调参。真实模型费用、人工支持率和收益仍未知。

依赖下载多次遇到 TLS EOF、连接中断和 Windows 临时文件锁。为继续验证，使用项目内缓存、低并发以及经 PyPI 官方 SHA-256 校验的 wheel；未关闭 TLS 验证，未替换为未验收的向量后端，未下载任何 Embedding 模型权重。失败安装不算作环境验证成功。

当前可独立验证的本地入口：

```powershell
python -m corecoder knowledge status --root . --json
python -m corecoder knowledge index --root . --dry-run --json
```

完整知识功能安装 `.[knowledge]`。可运行 `examples.knowledge_index_demo`、`examples.repo_qa_demo` 和 `examples.memory_demo`；前两者采用固定向量/生成替身，不访问模型服务。长期记忆边界及命令见 [design/08](../design/08-project-memory.md)。CI 已扩展知识/记忆 Windows+Linux 矩阵，但本地修改 CI 配置不等于云端执行成功。

## MCP 与交付收尾

- [三环境依赖/FTS5 检查](../../evals/results/next-stage/mcp-environments.json)：MCP 2.3.0，三个环境依赖一致性通过；实际 patch 版本和依赖数量分别记录，不假定解析结果完全相同。
- [Python 3.11](../../evals/results/next-stage/mcp-py311.json)、[Python 3.14](../../evals/results/next-stage/mcp-py314.json)：各 10 项通过（6 个真实 MCP 传输用例 + 4 个人工质量门禁检查）。
- [配对恢复](../../evals/results/next-stage/recovery-comparison.json)：相同固定后端、WriteFileTool、文件验证器、稳定 identity 和持久模型预算；三个故障点 × 两种入口 = 6 次 VERIFIED，重复副作用 0，终态恢复新增请求 0。两组都有持久预算适配，不声称图优于原运行时。
- [源码外 wheel 安装](../../evals/results/next-stage/wheel-install-checks.json)：基础/完整扩展分别在全新环境安装，核对导入位置和 8 个模板，包源码字节与工作树相同；实际运行四个演示。
- [基础 3.10 回归](../../evals/results/next-stage/base-final-py310.json)：466 passed、38 skipped；可选扩展缺失按能力跳过。[批量设置兼容补充](../../evals/results/next-stage/base-provider-settings-py310.json) 5 passed、1 skipped。
- [Embedding 地址/批量/usage 定向检查](../../evals/results/next-stage/provider-isolation-check.json)：37 passed、1 skipped。自定义 endpoint 纳入向量空间，禁止继承 Chat 地址；较小批量上限和仅 total_tokens 的服务用量受支持。

Windows 3.11 中文路径的初始 editable 安装失败保存在 [原始失败记录](../../evals/results/next-stage/mcp-py311-editable-failed.json)。Hatchling 改用精确导入 hook 后，UV 0.13.8 没有自动补齐新增的 `editables~=0.3` 运行依赖；按已生成的包元数据补齐依赖后真实 MCP 测试通过，该环境快照包含 editables。标准 wheel 不依赖该 hook。构建选项依据 [Hatch 官方文档](https://hatch.pypa.io/latest/config/build/)。

另用标准 pip 在全新 3.11 环境执行 [editable 安装](../../evals/results/next-stage/editable-pip-py311.json)，实际自动解析并安装 editables 成功。初始沙箱 pip 探针停在构建依赖下载阶段，已终止并用有超时/有限重试的正常网络权限重跑；不把该中断探针计为安装成功。

[冻结版本全量回归](../../evals/results/next-stage/full-mcp-py312.json)：**568 passed、1 skipped，147.03 秒**。最终 Ruff 和 README 检查通过。中间一轮在修正地址隔离时遇到 [源码统计漂移](../../evals/results/next-stage/full-mcp-source-count-drift-py312.json)，未删除检查；更新统计、停止修改生产源码后完整重跑通过。

最终扩展检查覆盖 SDK、模型/Prompt 网关、Embedding 配置和加载基础：[Python 3.11](../../evals/results/next-stage/integrations-final-py311.json) 74 passed、1 skipped；[Python 3.14](../../evals/results/next-stage/integrations-final-py314.json) 74 passed、1 skipped。独立客户端的 [hybrid 配置转发](../../evals/results/next-stage/mcp-client-environment.json) 使用真实 stdio 和固定向量，服务端查询计数为 1，真实模型请求为 0。[最终 Prompt 契约](../../evals/results/next-stage/prompts-final.json) v1/v2 各 14/14。

最终 wheel/sdist 构建与 [twine 校验](../../evals/results/next-stage/twine-current.json) 通过；源码外 14 个安装/演示步骤全部通过。完整源文件哈希、包哈希及报告入口见 [最终交付清单](../../evals/results/next-stage/delivery-manifest.json)。云端 CI 与真实质量不计入这些本地通过数。

用户已配置百炼 `text-embedding-v4` / 1024 维，当前数据目录已有部分成功的向量缓存；真实语料的质量实验、人工复核及完整费用验收仍未完成。后续步骤见 [质量实验协议](../quality-evaluation.md)。

## 本地关键词索引补充（2026-11-17）

远程 Embedding 接口可选。新增 `knowledge index --mode lexical`，首次建索引即可直接发布 SQLite/FTS5 关键词索引，无需 Key、模型请求或 Chroma，也不使用哈希向量替身。当前项目的 `corecoder.toml` 已将 `[knowledge] retrieval_mode` 设为 `"lexical"`；原有 Embedding 配置和已完成的向量缓存保留，按需用 `index --mode hybrid` 恢复语义索引。

纯本地快照以 `embedding_space: null` 与语义快照区分，保留版本、引用新鲜度、原子发布、增量更新和读者租约。关键词问答的来源刷新也走本地索引。真实答案生成仍需独立的 Chat 模型，关键词匹配不作为真实语义召回指标。

- 无 Chroma 的 Python 3.14 环境：7 项本地索引检查通过。
- [本次知识/Prompt/图/MCP/基础文档回归](../../evals/results/next-stage/lexical-index-py312.json)：192 passed、1 skipped，82.17 秒；文件 symlink 用例因 Windows 权限跳过。
- [当前项目本地索引与搜索](../../evals/results/next-stage/local-keyword-smoke.json)：阻断网络及 Chroma 导入后执行默认 index/status/search，模型请求为 0，原有 820 条向量缓存保留；`RunStore` 和 `resume_run` 均命中生产源码。自然语言长问句仍按关键词匹配，不据此声称语义相关性达标。

此前的交付清单、wheel/sdist 和全量回归报告对应此前冻结版本；本次追加变更以以上单独回归记录为准。

## 完整技术链路补充（2026-11-18）

- 新增 `examples.full_stack_demo`：复用已有模型适配、Prompt、LangChain、知识库、长期记忆、图和 MCP，串起三轮问答。11 项跨模块检查实际通过；OpenAI SDK 请求及 SSE 解析走进程内 HTTP 替身，向量采用固定替身，真实模型请求为 0。
- 新增 `evals.full_stack_runner`：汇总完整链路、Prompt v1/v2（各 14/14）、Agent 真实 MCP 调用和真实文件写入后的崩溃恢复。四组离线验收通过，报告为 `evals/results/full-stack/acceptance.json`。
- 真实 Chat 探针已执行：配置的 `deepseek-flash` 返回通过 Schema 与引用校验的回答，1 次请求、610 Token；这是固定小样本的 API 连接验收。独立 Embedding Key 缺失，Embedding 项为 `not_configured`、真实探针总状态为 `incomplete`，不作真实向量质量声明。无正文摘要见 `evals/results/full-stack/live-api-summary.json`，完整本地报告位于忽略目录 `.corecoder/acceptance/live.json`。
- 新增三项验收器测试：默认不会调用真实 API；某阶段失败不能被其他成功抵消且错误正文不进报告；Embedding 配置缺失时保留 Chat 结果并阻止总体验收通过。
- 全量回归首次暴露默认配置测试读取开发者 `.env` 的问题，已补充测试隔离；Windows 子进程取消/超时用例需在允许终止进程的环境中运行。回归结果另存 `evals/results/full-stack/`。
- 本轮最终全量离线回归为 **579 passed、1 skipped**（128.17 秒），在正常 Windows 进程终止权限下执行；跳过项为账户权限限制的文件 symlink 用例。新增验收器三项测试包含在内。Ruff 全仓检查、README benchmark 检查与源码 manifest 一致性检查均通过。
- 新增 `docs/full-stack.md`，更新演示入口与外层简历；真实语料 Recall、Prompt 质量优劣和记忆遵守率仍需独立实验与人工复核。
