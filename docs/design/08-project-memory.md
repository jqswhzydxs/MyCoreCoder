# 项目长期记忆

本模块使用 SQLite，不需要向量库或模型。基础 Python 3.10 安装可管理显式偏好；调用 `memory_extract` 链时才需要 `knowledge` 扩展。记忆是历史参考，不能作为当前源码事实、工具权限或 Verifier 配置。

## 身份、记录和时间

`MemoryService` 从可信 `ProjectScope` 接收 owner/project/canonical root。`MemoryStore` 将项目绑定到 root 的哈希，所有读取、写入和删除均限定 owner/project。共享数据库不会使不同项目共享偏好；本地隔离不构成服务器认证。

每条记录包含 ID、kind/key/content、来源类型/引用/哈希、证据 ID、状态、UTC 创建/过期时间、supersedes 和 write operation。状态为 candidate、active、superseded、expired、deleted；单独的 stale_reason 表示来源需要重新验证。时钟由宿主注入，过期边界为 `now >= expires_at`。

schema version 目前为 1。不兼容版本拒绝写入；首次建库和版本设置在同一事务中进行。SQL 参数绑定、唯一 active key、operation 唯一约束和 `BEGIN IMMEDIATE` 保证多连接竞争下的单次写入。数据库错误不会带出正文或路径。

## 来源与候选审核

- 用户显式请求可走确定性 `remember`，不读取 Chat Key。同 key 新偏好将旧记录标记 superseded，新记录的 supersedes 指向旧记录。
- 项目规则必须来自授权根内的稳定 `.py/.md` 文件。确定性规则内容必须实际出现在文件中，来源保存原始字节哈希。召回前再次回读；文件改变/删除时不注入，恢复原始内容后可重新验证。
- 经验只能绑定宿主提供的项目 RunStore。服务回查真实记录的 verified/passed 状态、完整 Verifier evidence 和 evidence ID 哈希，不接受调用方传入的状态字典。旧全局 RunStore 不会自动获得项目归属；宿主负责提供可信项目存储，后续 RuntimeBridge 负责绑定。
- `stage_extraction` 使用现有 `memory_extract.v2` 与共享 ModelGateway，仅保存 candidate。来源引用和证据集合必须来自可信调用方；激活需显式 `approve` 并再次验证来源。用户来源必须匹配请求哈希，文件来源必须使用真实文件全文，运行来源必须使用已保存的运行摘要。
- 凭据模式、私钥和敏感赋值被拒绝；不能写入 permission/tools/command 等权限配置 key。此模式检查是保守的输入门槛，不承诺识别所有秘密；记忆也从不修改 Permission。

模型候选的 schema 成立、源码哈希有效、运行曾经 VERIFIED，都不代表候选语义必然正确。首版保留明确审核边界。

## 召回和删除

召回先过滤作用域、active、TTL 和当前来源，再按关键词、来源类型和时间排序。偏好可跨任务召回，经验需要关键词相关。最多五条、约 1000 Token，估算包含来源与时间元数据；超预算时跳过整条，不截断成另一条指令。

当前指令优先于历史偏好。程序可传 `current_keys` 排除冲突 key；CLI 对应 `--current-memory-key`。其余自然语言冲突通过 Prompt 明确当前请求优先，真实模型遵守程度需另行评测。计划通过 `plan_constraints` 接收带 `historical_memory` 类型的约束，现有可信约束排在前面。

问答默认通过同根 MemoryService 召回，可用 `ask --no-memory` 关闭。生成后再次检查记忆上下文；期间删除/变更则丢弃草稿，最多重新生成一次。历史记忆不进入源码 evidence ID 集合，不能被当作源码引用。

`forget` 在事务内清空正文、key、来源和证据，只保留必要的无正文 tombstone、operation 与请求哈希。相同 operation 重放返回 deleted，不会重新激活。没有进程内检索缓存；每次召回读取当前数据库。SQLite 使用 secure_delete；这不是对磁盘备份、文件系统历史版本的安全擦除承诺。新的显式用户请求可以使用新 operation 再保存。

`clear --project-id` 要求精确当前项目 ID，只逐条删除该作用域，不递归删除磁盘目录。失败可重入。

## 可运行命令与验证

在项目根运行，管理命令无需 Key、网络或 Embedding 模型：

```powershell
python -m corecoder memory remember "回答优先使用中文" --root . --key response.language
python -m corecoder memory list --root . --json
python -m corecoder memory show MEMORY_ID --root . --json
python -m corecoder memory forget MEMORY_ID --root . --json
python -m examples.memory_demo
python -m evals.memory_runner
```

根目录优先级为 CLI `--root`、`CORECODER_MEMORY_ROOT`、`CORECODER_KNOWLEDGE_ROOT`、cwd。数据目录优先级为 CLI `--data-root`、`CORECODER_MEMORY_DATA_ROOT`、`corecoder.toml` 的 `[memory].persist_directory`、`.corecoder/memory`。简单偏好不调用提取模型。

固定评测在执行前保存了 20 组序列，使用临时目录、SQLite、可控 UTC 时钟和固定模型。报告区分 memory off/on，记录上下文精确匹配、必要约束召回、删除残留和项目越界。它验证生命周期与注入上下文，不衡量真实模型的语义提取或指令遵守率。
