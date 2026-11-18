# 大模型工程升级实施记录

日期：2026-11-13（Asia/Singapore）。本记录对应原[执行计划](llm-engineering-execution-plan.zh-CN.md)，原计划保留为设计基准，未将未来里程碑改标为完成。

后续工作已开始，逐项状态与新证据另见[下一阶段实施记录](next-stage-status.zh-CN.md)。本文保留首轮历史受测结果，不将后续未验收实现混入首轮通过数字。

## 本轮范围

本轮完成基线修复和 M1 的离线模型/Prompt 链路。M0 中面向全部扩展的依赖验证、知识/记忆/图契约与问答开发集，以及 M2–M7 的业务能力仍需后续实施。

开始时已有 `README.md` 删除、`README_CN.md` 未跟踪、`docs/roadmap/` 未跟踪。本轮补建包元数据需要的英文 README，保留中文文档及执行计划；仅补充中文文档的新增功能入口与当前源码统计，并同步文章中的源码行数引用。

| 实施项 | 实际状态 |
|---|---|
| README、发布说明、构建元数据与数据目录忽略 | 已修复 |
| ADR-0004 | 已记录可选集成边界；保留显式 Agent 循环 |
| ModelGateway | 已实现单次后端、错误分类、有限重试、请求指标与工具消息配对 |
| 400 参数适配 | 仅对明确引用的 max_tokens、temperature、stream_options 调整；重试仍计入总次数 |
| BudgetLedger | 跨 Prompt/修复请求共享次数、Token、deadline 与显式费率成本预算 |
| LangChain | 实际运行 ChatPromptTemplate → Gateway Runnable → Pydantic 校验链 |
| Prompt | 问答、计划、记忆候选各有 v1/v2，包内资源、输入契约与 SHA-256 |
| 输出治理 | 禁止额外权限/身份字段、验证 evidence ID 和 memory source_ref、最多一次结构修复 |
| 配置 | Embedding 配置独立于 Chat；Config repr 隐藏 API Key |
| 隐私 | 新链路关闭 LangSmith tracing；调用记录不含 Prompt、工具正文或异常响应体 |
| 离线对照、示例与扩展 CI | 已实现；CI 配置不代表远端已经执行通过 |
| 仓库索引、语义检索、引用新鲜度 | 未实现，属于 M2/M3 |
| 长期记忆与自动经验写入 | 未实现；当前仅输出候选，不提交存储 |
| LangGraph、RuntimeBridge、知识 MCP | 未实现，属于后续里程碑 |

## 基线与验证证据

初次在 Windows 沙箱内运行基础离线套件：**399 passed、6 failed**。其中 4 项与缺失 README/发布资料有关；另外 2 项 Shell 超时/取消检查因沙箱拒绝 `taskkill` 而失败。独立探针返回 `ERROR: Access denied`，在常规进程权限和项目内独立临时目录下，两项均通过，因此本轮未修改 Shell 终止逻辑。

本轮新增离线集成测试覆盖认证失败、参数错误、429/5xx、连接中断、SDK 重试关闭、请求预算、未知 usage/cost、并发预算预留、流关闭与不完整工具调用、消息配对、结构修复、证据伪造、身份注入、来源伪造、模板数据隔离和关闭 tracing。

最终在 Windows / CPython 3.14.8 上验证：

| 检查 | 结果 |
|---|---|
| 完整离线回归（排除 live smoke） | **436 passed** |
| Python 3.10.11 基础安装，不安装 LangChain | **425 passed、1 skipped**（可选 Prompt 测试模块） |
| Ruff：corecoder、tests、examples、evals | 通过 |
| README benchmark 与源码计数检查 | 通过 |
| pip check | 通过 |
| sdist / wheel 构建与 twine check | 通过 |
| wheel 独立目录导入与 Prompt 链 | 通过；6 个模板资源齐全 |

本轮没有创建 Git 提交。可靠性报告中的 HEAD 为 `50e5e7df168d708b45ed0eb43d7fffaa9edff5d0`，源码改动还需结合 Prompt 报告中的 source manifest 识别，不能将该 HEAD 单独当作本轮实现版本。

原有可靠性评测在相同场景、脚本与配置下重新执行：baseline 和 candidate 各 **40/40** 观测符合预期，安全门禁通过。报告保存于：

- [可靠性 baseline](../../evals/results/llm-engineering/reliability-baseline.json)
- [可靠性 candidate](../../evals/results/llm-engineering/reliability-candidate.json)
- [安全门禁比较](../../evals/results/llm-engineering/reliability-comparison.md)
- [Prompt v1/v2 对照](../../evals/results/llm-engineering/prompts-offline.md)

Prompt 对照两版各 **14/14** 个契约用例符合预期，固定响应完全相同，报告含测试集哈希、源码清单哈希、模板哈希与依赖版本。这不证明真实模型的回答质量、记忆质量或检索质量提升。没有调用真实 Chat/Embedding API，未使用用户凭据，未生成线上质量或真实费用数字。

## 安装与复现

基础包仍声明 Python 3.10+。可选 `langchain-core==1.8.0` 本身要求 Python 3.11+，因此知识/Prompt 扩展要求 Python 3.11+，不对 Python 3.10 静默跳过所需依赖。

```powershell
python -m pip install -e ".[dev,knowledge]"
python -m examples.prompt_gateway_demo
python -m evals.prompt_runner --compare v1 v2 --offline
python -m pytest tests --ignore=tests/test_live_smoke.py -q
python -m ruff check corecoder tests examples evals
python -m evals.readme_benchmark --check
python -m pip check
```

Windows / CPython 3.14.8 的基础、Prompt、测试和打包工具环境冻结于 [requirements/knowledge-py314-windows.lock.txt](../../requirements/knowledge-py314-windows.lock.txt)。它是本机环境的完整版本快照，不是已经验证过的跨平台锁。干净的同平台环境可先安装该文件，再安装项目。扩展 CI 配置为 Python 3.11、3.12、3.14；未安装扩展时基础套件可跳过 Prompt 集成测试，扩展 CI 则强制要求依赖存在。

另已创建干净的 CPython 3.10.11 环境，从本轮 wheel 安装基础包并完成回归，`pip check` 通过且确认未安装 LangChain。该环境使用 OpenAI SDK 1.109.1 / Pydantic 2.13.5，其完整版本快照为 [requirements/base-py310-windows.lock.txt](../../requirements/base-py310-windows.lock.txt)。这验证了受测版本组合，不等同于所有可解析依赖版本都已测试。扩展的 Python 3.11/3.12 路径尚待 CI 执行。

打包命令：

```powershell
python -m pip install build twine
python -m build
python -m twine check dist/*
```

## 边界与下一阶段

Token 预检使用 UTF-8 字节数作保守估算，不冒充供应商 tokenizer。usage 不可用或请求失败时按预留额度计量，并将费用显示为未知；严格美元预算必须提供可信费率。deadline 在请求、重试与流片段边界检查，并传递给底层超时，无法撤销供应商已经处理的请求。

输出通过 Schema 和证据 ID 检查仅表示结构成立，不能证明语义结论正确或源码引用仍新鲜。当前记忆输出始终为候选；来源校验、敏感数据过滤、VERIFIED 经验门槛、项目隔离、TTL 与删除必须由后续 MemoryService 完成。

下一阶段依照原计划实现 Python/Markdown 安全加载、AST/标题切分、增量 snapshot、独立 EmbeddingProvider 和 FTS5/向量混合检索，再接入长期记忆、图恢复与 MCP 服务。Chroma/LangGraph/MCP 的安装兼容性与真实模型小样本仍未验收。

后续逐项任务、接口与验收方式见[后续阶段详细执行文档](next-stage-execution-plan.zh-CN.md)。该文档是待实施清单，不改变本记录中的已完成范围。
