# CoreCoder fork 实验基线

记录日期：2026-11-03（Asia/Singapore）。

## 基线身份

| 项目 | 值 |
|---|---|
| 上游仓库 | `https://github.com/he-yufeng/CoreCoder.git` |
| 上游版本 | `0.8.0` |
| 上游提交 | `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0` |
| 本地分支 | `main`，任务开始时与本地 `origin/main` 指向同一提交 |
| Python 包规模 | 33 个 `.py` 文件；8,157 物理行；7,188 净行 |
| Agent 兼容性 | `Agent.chat()` 仍返回字符串，并对未验证结果添加明确标记；`Agent.run()`、Trace、ToolSpec/ToolResult、RunBudget、Verifier 和可选 RunStore 均保留旧兼容路径 |

按任务约束，本次没有创建 commit 或 push。当前可复现身份因此是“上述上游提交 + 本文记录的 fork 工作树增量”；在用户自行提交前，不应把未提交工作树描述成一个新的 commit。

## 工作树来源边界

任务开始前已经存在以下内容，本次全部保留：

- 上游受跟踪文件的本地修改：`article/05-parallel-and-subagents.md`，以及 `corecoder/checkpoints.py`、`cli.py`、`config.py`、`demo.py`、`hooks.py`、`mcp.py`、`shell.py`。七个 Python 文件只在模块 docstring 前增加了文件名行；文章文件末尾有一个空格差异。
- 未跟踪文件：`.idea/` 和 `examples/learning_loop_trace.py`。本基线将 `.idea/` 加入 `.gitignore`，但没有删除目录；学习示例保留并作为 fork 的离线教材。

本 fork 在文档基线之后依次加入离线 Eval Harness、可选 Trace、显式 `RunState` / `Agent.run()`、统一 ToolSpec/ToolResult 工具协议、统一 RunBudget / 协作式取消 / 无进展检测、元数据驱动的安全 ToolScheduler、由外部证据驱动的可插拔 Verifier，以及事务化 SQLite RunStore 和保守崩溃恢复；上游已有能力与本 fork 新增内容的归属规则见 `README_CN.md` 的“二次开发说明”。

## 已验证环境

| 组件 | 版本 |
|---|---|
| 操作系统 | Windows 11，build 26100（`Windows-11-10.0.26100-SP0`） |
| Python | CPython 3.14.8 |
| CoreCoder | 0.8.0，可编辑安装 |
| openai | 3.37.0 |
| rich | 15.0.0 |
| prompt_toolkit | 3.0.53 |
| python-dotenv | 1.2.4 |
| pytest | 9.1.1 |
| Ruff | 0.17.2 |
| LiteLLM | 未安装（可选依赖） |

`python -m pip check` 返回 `No broken requirements found`。项目没有依赖锁文件；上表记录的是本次实际通过验证的主要版本，不代表项目声明允许的所有版本组合都已验证。

## 另一台机器上的复现步骤

先取得包含本基线增量的 fork 工作树或用户之后创建的 baseline commit，并确认其上游基点是：

```powershell
git merge-base HEAD bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0
```

在仓库根目录创建隔离环境并安装本次验证的主要版本：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . openai==3.37.0 rich==15.0.0 prompt_toolkit==3.0.53 python-dotenv==1.2.4 pytest==9.1.1 ruff==0.17.2
$env:PYTHONUTF8 = '1'
$env:PATH = (Join-Path (Get-Location) '.venv\Scripts') + [IO.Path]::PathSeparator + $env:PATH
.\.venv\Scripts\pytest.exe tests --ignore=tests/test_live_smoke.py -q
.\.venv\Scripts\ruff.exe check corecoder tests examples
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m evals.runner --suite all --variant baseline
.\.venv\Scripts\python.exe -m evals.runner --suite all --variant candidate
.\.venv\Scripts\python.exe -m evals.compare baseline candidate
```

项目声明支持 Python 3.10 及以上；为了复现本文的具体结果，应优先使用 CPython 3.14.8。若使用 POSIX shell，将虚拟环境路径和环境变量语法换成对应平台形式即可。

## 验证结果

| 检查 | 结果 |
|---|---|
| 离线测试 | **327 passed** |
| 测试数量 | 327 项（显式排除 `tests/test_live_smoke.py`；其中 9 项验证 40 条 baseline/candidate 离线 Eval Harness 用例、故障注入、对照报告与安全门禁，10 项验证可选 Trace，11 项验证显式状态机，13 项验证工具协议，14 项验证预算、取消和无进展检测，12 项验证 ToolScheduler，13 项验证可插拔 Verifier，15 项验证持久化与崩溃恢复；本次另增加 1 项离线 crash/resume Demo 验收和 2 项投递文档/README benchmark 一致性检查） |
| Ruff | **All checks passed!** |
| 依赖一致性 | **No broken requirements found** |

最初的文档基线修复前，同一离线命令的结果是 225 passed、2 failed；失败仅来自 README 包体物理行数及 article 精确行数引用漂移。修正文档数字后原有 227 项测试通过，本 fork 随后为完全离线 Eval Harness 增加 9 项测试，为可选 Trace 增加 10 项测试，为显式状态机增加 11 项测试，为工具协议增加 13 项测试，为预算、取消和无进展检测增加 14 项测试，为 ToolScheduler 增加 12 项测试，为可插拔 Verifier 增加 13 项测试，为持久化与崩溃恢复增加 15 项测试，未删除测试。

## 已知限制

- 没有执行真实模型测试。原因是本任务默认只允许离线验证，且 `tests/test_live_smoke.py` 会访问外部模型服务；为保证无网络、无费用、无凭据泄露的可复现基线，该文件被显式排除，期间没有读取或打印 API Key。
- 没有验证 LiteLLM 的真实 provider、MCP 外部服务或任何需要网络的集成；相应单元测试使用本地模拟或伪服务器。
- 只在 Windows 11 / CPython 3.14.8 上运行，尚未复跑仓库 CI 的其他操作系统与 Python 版本矩阵。
- 项目没有锁定全部直接和传递依赖。本文固定了本次环境的主要包版本，但构建后端和传递依赖仍可能因安装时间而变化；严格长期复现需要后续增加锁文件或带哈希的约束文件。
- 当前 baseline 尚未形成用户提交，因此另一台机器必须取得当前 fork 工作树快照，或等待用户把这些增量提交后再按 commit 检出。
- 当前没有真实模型或生产任务 benchmark；已签入的 40 条确定性离线 Eval 只统计本 fork 的可靠性改造，并固定任务集、脚本模型、Prompt、代码基点和运行次数。
- Eval Harness 的 40 条用例使用 `FaultInjectingLLM` 和确定性故障；延迟是模拟工作量而非墙钟基准，Token 是脚本计数，成本使用固定的测试费率。报告不声称真实模型上的统计显著性。
- 内置 Schema 校验器覆盖本项目与常见 MCP 工具使用的 JSON Schema 子集，不是完整 Draft 2020-12 实现。
- 通用超时通过 daemon 线程返回受控结果，不能强制终止已经进入系统调用的第三方 Python 工具；bash 与 MCP 使用各自可控的原生超时。
- 协作式取消和 deadline 在模型/工具安全边界检查，不能强制抢占正在阻塞的第三方调用；要获得严格墙钟上限，工具或 provider 仍需支持原生超时/进程隔离。
- 未知价格模型不会被当作零成本；结果与 Trace 标记 `cost_unavailable`，但 `max_cost_usd` 无法在部署方补充可信价格前强制执行。
- ToolScheduler 对混合批次采用保守整批串行，不会把安全子集拆成并发 wave；线程中的只读调用也只能协作式取消，不能强制终止已经进入阻塞系统调用的第三方实现。
- 命令型 Verifier 只接受可信调用方配置并经过 `Permission`，但最终仍依赖本机 shell，不构成沙箱；不应把模型文本或其他不可信字符串直接用作验证命令。
- `FileStateVerifier` 第一版只覆盖存在性、文本/哈希条件和未修改快照；更复杂的语义正确性仍需要调用方提供专用 Verifier。旧的布尔 callable 会通过兼容适配器生成可追溯决策证据，但其可信度由调用方负责。
- RunStore 对 `STARTED` 文件写入可用目标哈希确认结果；bash、MCP 及其他无法查询外部状态的非幂等副作用会保守进入 `UNKNOWN`，不会自动重放，仍需人工检查或服务端幂等/状态查询能力。
- Verifier 对象本身不序列化进 SQLite。若进程在验证阶段退出，库调用方恢复时应再次传入同一可信 Verifier；CLI 在没有该配置时只会得到未验证结果，不会冒充成功。
