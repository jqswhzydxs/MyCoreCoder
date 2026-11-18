# 持久图工作流与现有运行时

图扩展负责高层阶段，编码工具仍只由现有 Agent/ToolScheduler/Permission/RunStore/Verifier 执行。安装 `.[knowledge,graph]`。当前组合为 LangGraph 1.2.20、SQLite checkpointer 3.1.3；Windows/Python 3.12 已运行真实图、检查点和文件写入恢复，最终环境矩阵与综合验收另行记录。

## 状态和 payload

StateGraph 包含 prepare、retrieve、recall、plan、answer、execute、insufficient、review、commit_memory、finalize。问答、代码修改、缺资料和显式记忆有实际条件分支，节点 max_attempts=1，不自动重跑整个编码任务。问答复用 RepositoryAnswerer/BaseRetriever 和新鲜度检查；编码复用 RuntimeBridge。

检查点只含版本、作用域、thread/turn/workflow、request/retrieval/memory/plan/result 引用、snapshot、child ID、阶段、独立状态和无正文预算。问题、片段、约束、计划和结果存到受控 PayloadStore，绑定 owner/project/root、用途、哈希、工作流生命周期和到期时间。payload 缺失、过期或损坏时要求处理，不换输入继续。

SQLite checkpoint 使用 JSON 序列化并拒绝其他类型；错误通道保存固定标识，不保存 provider exception 正文。Verifier、客户端和密钥不进检查点。LangSmith tracing 在调用上下文禁用。受控 payload 是本地私有数据，不应发布到仓库。

## 身份和认领

工作流由 project/thread/turn 生成稳定 ID。SQLite operation 在执行前绑定请求、原预算、可信配置和 request_ref；相同 turn 换输入/配置被拒绝。新的用户回合使用新 turn。

RuntimeBridge 以 project/thread/turn/plan_hash/attempt 建立 operation，先保存 child_run_id，再调用 `Agent.run_once`。plan hash 覆盖严格校验后的计划、证据集合、记忆上下文、请求和可信配置。新子运行开始前再次验证证据与记忆，已有子运行按原任务事实恢复。

新增公开契约为 `Agent.run_once(task_spec, run_id=..., configuration_hash=...)`：新身份启动；相同输入重入查询/恢复；任务、初始历史、工具契约、模型或可信配置变化被拒绝。宿主必须将 Verifier 策略、实际工具配置和输入版本纳入 configuration_hash；跨进程恢复非空初始历史时提供一致版本。Verifier 对象本身不被序列化。

终态读取 RunStore，不再调用模型、工具或 Verifier。历史 VERIFIED 表示该运行当时满足条件，不表示文件永远不变。自动恢复默认 `resume_paused=False`。旧 run/chat/resume_run 保持兼容；无 input identity 的历史运行不被新接口悄悄接管。

SQLite 唯一约束绑定映射；OS 文件锁排斥同主机活跃拥有者，进程退出由系统释放。慢工具不会因 lease 超时被另一进程抢占。本实现不承诺网络文件系统或多机互斥。claim 文件只含一个占位字节，文件名为哈希。

## 预算与恢复边界

DurableBudgetLedger 在发送前持久化预留、原限额和 UTC deadline。Embedding、planner、answer、结构修复、可选记忆提取、child Agent 和摘要使用同一总账，各自使用显式费率。AgentLLMBridge 保持 `.chat()`/usage/cost 接口，ContextManager 用 summarize 入口计费。chat_once 缓冲返回，回调发生在请求完成后，不承诺实时 Token 流；旧流式路径保留。

持有 workflow claim 的恢复方才能把遗留预留转为未知消耗，按上限保守记账。usage 丢失不变为零费用，结算可重入但不同 usage 的重复结算被拒绝。恢复不重置额度/deadline；预算记录丢失时拒绝重新分配。RunBudget 继续约束工具/轮次，全局报告不把子账费用再加一次。

受测恢复包括映射已写/子运行未建、终态/图未确认、文件已写/回执未存、UNKNOWN 外部效果、PAUSED、丢失 usage、并发恢复、取消、预算停止、记忆已写/图未确认，以及缺 payload/错项目/策略变化。文件对账沿用 RunStore 哈希协议，UNKNOWN 非幂等效果不重放；不声明跨 SQLite/外部服务的全局 exactly-once。

PAUSED 自动恢复保持暂停，显式 workflow resume 才表达继续意图。继续仍受旧运行时限制：待写正文已被脱敏且无法重建时返回 NEEDS_ACTION，不能猜测原内容。取消、预算耗尽和需要处理的状态不会被当作普通失败自动重跑。

workflow_status、answer_status 和 code_run_status 分开。图结束可对应编码 unverified。可选记忆保存失败单独报告，不撤销子运行 VERIFIED；自动经验先保存 candidate，仍需显式审核。

## CLI 和可信配置

```powershell
python -m corecoder ask "RunStore 如何恢复？" --root . --engine graph --json
python -m corecoder run "完成指定修改" --root . --engine graph --json
python -m corecoder workflow status WORKFLOW_ID --root . --json
python -m corecoder workflow resume WORKFLOW_ID --root . --json
python -m examples.graph_resume_demo
```

status 本地查询不构造 Chat。执行/恢复需提供与原运行一致的模型、Embedding、权限和项目配置，先前使用 CLI 覆盖时恢复也保留相同覆盖。`--offline-fixture` 明确选择固定后端；该后端问答是抽取演示，编码分支不会自行实现修改。

编码命令须从原项目根运行，保留已有工具 cwd 语义。默认沿用保守 Permission；`--yes` 与旧 CLI 的显式全允许含义相同，恢复不隐式增加权限。首版提供阶段事件、结果与取消传递，未声明运行中 APPEND/REDIRECT。

Verifier 来自可信 corecoder.toml，不由模型选择。例如：

```toml
[workflow]
success_criteria = ["artifact.md contains the expected result"]

[workflow.verification]
policy_id = "artifact-v1"
type = "file_state"
path = "artifact.md"
content_equals = "ready"
```

也支持 `type="test_command"`，由项目配置提供 command 与最多 300 秒的 timeout_seconds，仍通过现有 Permission 执行。改变实际策略应修改可信配置并创建新 turn；缺失策略不由模型自报通过填补。

工作流目录优先级为 CLI `--workflow-data-root`、`CORECODER_WORKFLOW_DATA_ROOT`、`[workflow].persist_directory`、`.corecoder/workflows`。ask graph 使用项目/环境目录配置；运行时和记忆保持同一 canonical root。

tests/workflows 使用固定后端、禁止网络和临时项目。真实演示实际写文件、注入中断、重开服务并检查物理写入次数。CI 配置增加显式扩展门禁，但不代表云端已执行。最终三环境、固定恢复矩阵报告、wheel 和 MCP 集成仍需完成。

参考：[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[重试与副作用](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)、[SqliteSaver](https://reference.langchain.com/python/langgraph.checkpoint.sqlite/SqliteSaver)。
