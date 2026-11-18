# 质量实验与人工复核

默认离线测试和固定向量不能验收真实 Recall@5 或回答支持率。40 题已冻结为 24 开发/16 保留，gold 用源码符号或文档标题定位，Recall 是相关片段命中比例的宏平均；保留集不能逐题调参。

现有报告分别记录三模式检索、记忆 off/on、Prompt 契约和图恢复。`evals.quality_runner` 另行支持真实检索及生成实验；没有 `--live` 时只用固定替身，不读取模型凭据。

```powershell
python -m evals.quality_runner --limit 2
```

输出默认写在被 Git 忽略的 `.corecoder/quality/`，包含问题、答案、实际提供的证据和本地人工标注模板。它会复制授权语料到临时目录，使用真实 Chroma/FTS5；不将本地模型回答或源码正文自动写入受版本控制的评测结果。

## 显式真实实验

在环境变量或项目 `corecoder.toml` 中配置独立 Embedding 模型、维度、地址和专用 `api_key_env`；Chat 使用自己的 provider/model/key。不要把 Key 写入命令参数或报告。Embedding 不继承 Chat 的地址与 Key，也不自动读取 `.env`；使用导出的环境变量或 TOML 中的 Key 环境变量名。配置字段见 [知识设计](design/06-repository-knowledge.md)。

当前按用户要求保持真实模型配置为空。首次接入建议从百炼 `text-embedding-v4`、1024 维开始：它支持中文、英语和编程语言，兼容接口允许显式 `dimensions` 和 `encoding_format="float"`，同步请求每批最多 10 条。项目已支持 `[embedding] batch_size=10`；示例 `examples/embedding-bailian.example.toml` 的模型与地址为空，不会自动启用服务。这里是接口兼容性选择，不是本仓库语料上的质量胜出结论。[官方接口与限制](https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai)、[官方维度建议](https://help.aliyun.com/zh/model-studio/embedding-rerank-model/)。

开通后使用控制台显示的业务空间/地域地址；北京与新加坡的 Key、域名需对应。新域名含业务空间 ID，不要照抄其他项目的 ID。改变模型或维度会建立新向量空间，不能把固定向量索引当成真实模型索引继续使用。

先做有上限的小样本，再按需要扩大：

```powershell
python -m evals.quality_runner --live --split dev --limit 4 --max-requests 80 --max-tokens 2000000 --deadline 300
```

只有显式 `--live` 会访问配置的服务并可能收费。可选 `--max-cost` 要求可信 Chat 和 Embedding 费率；缺费率不能承诺金额上限。所有批次、查询、生成及最多一次结构修复共用账本；SDK 重试关闭，runner 的模型网关设单次尝试。请求、Token、墙钟和费用达到上限会保存失败类别，未知 usage/费用不会记为免费。预算耗尽的部分报告不能通过最终质量门禁。

每次实验固定语料、模型、问题、topK=5/10、生成上限及重复数。支持 `--repetitions 3`；完整离线/真实小样本的界限以报告 case_ids 为准，不能把局部结果外推到全部题目。真实回答含敏感内容时继续留在本地忽略目录。

对照项：

- lexical/vector/hybrid：同一索引和问题，记录每题 gold 命中、首个相关 rank、延迟。
- repo_answer v1/v2：使用同一次冻结的 hybrid 检索，不用不同检索结果解释 Prompt 优劣。
- evidence off/on：关闭检索上下文时仍使用相同的“仅依据证据”Prompt，因此预期拒答。这项衡量证据约束，不等价于允许模型凭记忆作答的 closed-book 基线。
- 冷/热索引：报告实测耗时，零变化的 document Embedding 请求必须为 0。

## 人工标注

`live.labels.pending.json` 的 `report_sha256` 绑定完整实验，避免把旧标注应用于变更后的答案。由实际评审者填写 reviewer，并逐项将 null 改成布尔值：

- `supported`：该回答中的仓库事实是否全部由实际证据支持。引用 ID 合法不代表语义支持。
- `correct`：整体是否正确处理问题，包括不确定性与适当拒答。
- `refusal_correct`：无答案问题是否正确拒答；对有答案问题仍填写布尔值，但不计入无答案分母。

复制模板为 `live.labels.json` 后运行：

```powershell
python -m evals.quality_runner --review .corecoder/quality/live.json --labels .corecoder/quality/live.labels.json
```

校验要求所有答案都有不重复的布尔标注、所有 Schema 有效、真实实验完整无预算/服务失败。门禁使用 hybrid 宏 Recall≥0.80、v2 开启检索的已回答样本人工支持率≥90%、无答案正确处理≥90%，同时报告分子分母；没有已回答或无答案样本时不能验收。fixture 模式即使填满标注也不能通过真实质量门禁。评测器不能验证填写者身份，人工标注来源需在评审时另行确认。

跨项目、删除复活、重复副作用、伪造引用、源码失效和测试凭据泄漏等硬门禁继续由独立测试和固定契约报告验证，不能用平均质量指标抵消。CI 配置存在不代表云端已执行。当前真实质量仍未验收。
