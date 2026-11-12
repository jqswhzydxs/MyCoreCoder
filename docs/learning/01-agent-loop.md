# 第一课：一次用户输入，为什么会多次调用模型

本课最初按上游提交 `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0` 编写，下面的行号与伪代码保留为 inherited baseline。当前 fork 已让 `Agent.chat()` 委托给 `Agent.run()`，并在 `state.py` 中显式记录状态、证据与退出原因；阅读当前实现时应同时对照这两个文件。

本课先集中读四个位置：上游 `agent.py:25` 的构造函数、`agent.py:53` 的消息拼装、`agent.py:70` 的主循环，以及 `llm.py:20` 的响应数据结构。

目标不是记住函数名，而是能跟踪：**用户输入 → 模型提出工具调用 → 程序执行或拒绝 → 结果回填 → 模型下一次回答**。

## 1. 先分清三个角色

| 角色 | 它决定或执行什么 | 对应代码 |
|---|---|---|
| 模型 | 根据收到的上下文生成回答，或提出工具名称和参数 | `llm.py:147` 的 `LLM.chat()` |
| 控制器 Agent | 保存历史、选择执行分支、调用工具、回填结果、控制轮数 | `agent.py:70` 的 `Agent.chat()` |
| 工具 | 真正读文件、改文件、启动命令，返回文本 | 例如 `tools/read.py:34` 的 `execute()` |

模型提出 `read_file({"file_path": "note.txt"})` 时，还没有读取磁盘。读磁盘发生在本地工具的 `Path.read_text()`。

`on_token`、`on_tool`、`on_reasoning` 是展示用回调。特别是 `on_tool` 在权限检查和实际执行之前触发，终端出现工具名称只能说明“模型请求了工具”。

## 2. Agent 初始化时记住了什么

`Agent.__init__()` 里最先关注下面几项：

```python
self.llm = llm
self.tools = tools if tools is not None else ALL_TOOLS
self._tool_by_name = {t.name: t for t in self.tools}
self.messages: list[dict] = []
self.context = ContextManager(max_tokens=max_context_tokens)
self.max_rounds = max_rounds
self._system = system_prompt(self.tools)
```

- `llm`：可替换的模型调用对象。离线演示传 `ScriptedLLM`，也能走同一个控制循环。
- `tools`：本 Agent 拿到的工具对象；`_tool_by_name` 是分发时用的名字到对象的字典。
- `messages`：持续积累的用户、assistant 和 tool 消息；连续两次 `chat()` 会接着同一段历史。
- `context`：在指定时间点检查历史长度，必要时压缩。
- `max_rounds`：一次 `chat()` 的主循环上限，默认 50。
- `_system`：初始化时生成的基础系统提示词。

`tools=[]` 表示不给工具；只有 `tools=None` 才选默认工具。这是一个值得看懂的 Python 判断。

第一轮暂时跳过子 Agent 和 todo 的初始化细节；后续只需记住 `_full_messages()` 会把计划模式说明和当前 todo 状态补进 system 消息。

## 3. 模型实际收到什么

`agent.py:53` 的 `_full_messages()` 返回：

```python
[{"role": "system", "content": system}] + self.messages
```

system 并不保存在 `self.messages` 里，而是每次发送请求时放到最前面。`agent.py:67` 的 `_tool_schemas()` 则逐个调用工具的 `schema()`，生成模型可读的工具名称、说明和参数结构。

所以一次模型请求有两份需要区分的输入：

```text
messages：系统指令 + 历史对话 + 之前的工具结果
tools：允许提出的工具及参数说明
```

Python 的 `execute()` 不会被当作函数代码发送给模型；`tools/base.py:23` 发的是 JSON 格式的调用说明。

## 4. 把主循环缩成八步

下面是帮助理解的伪代码，省略并行、hooks 和中断处理，不用于替换原实现：

```python
append_user_message(user_input)
maybe_compress_history()
for _ in range(max_rounds):
    response = ask_model(system_and_history, tool_schemas)
    append_assistant_message(response)
    if not response.tool_calls:
        return response.content
    execute_or_refuse_calls_and_append_tool_results(response.tool_calls)
    maybe_compress_history()
return "(reached maximum tool-call rounds)"
```

对应原文件的阅读顺序：

| 位置 | 本次发生什么 |
|---|---|
| `agent.py:72` | 把本次输入作为 user 消息追加到历史 |
| `agent.py:73` | 请求前检查是否需要压缩 |
| `agent.py:75` | 进入有上限的模型调用循环 |
| `agent.py:76` | 构造完整消息与工具说明，调用模型接口 |
| `agent.py:84` | 若没有工具调用，保存 assistant 消息并返回文本 |
| `agent.py:90` | 若有工具调用，先保存包含 tool_calls 的 assistant 消息 |
| `agent.py:97` | 单工具：pre hooks → 计划模式 / 权限检查；放行才执行，再运行 post hooks |
| `agent.py:101` | 把执行结果或拒绝原因保存为 tool 消息 |
| `agent.py:108` | 多工具走线程池分支，随后按调用列表顺序回填结果 |
| `agent.py:122` | 工具输出加入后再次检查压缩，然后进入下一轮模型请求 |
| `agent.py:124` | 用尽主循环次数，返回上限提示 |

判断结束用的是 **有没有 `tool_calls`**。一条响应可以同时有文本和工具调用；这时文本不会让控制循环提前退出。

“模型结束了这一轮任务”也不等于“任务已通过独立验证”。原版靠模型选择停止；是否真的改对，需要看文件、命令退出码与测试结果。

## 5. 用一个读文件任务展开消息变化

假设用户说“读取 note.txt，告诉我内容”，文件只有一行 `hello agent`。

第一次请求时，`self.messages` 里只有用户输入；发送时额外加 system。模型返回一个工具调用，经 `LLMResponse.message` 转换后存为：

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [{
    "id": "learn-1",
    "type": "function",
    "function": {
      "name": "read_file",
      "arguments": "{\"file_path\": \"note.txt\"}"
    }
  }]
}
```

这里 `function.arguments` 在消息协议中是 **JSON 字符串**；进入 Python 的 `ToolCall.arguments` 后则是 **dict**。不要混淆这两种表示。

`_exec_tool()` 找到 `ReadFileTool`，执行完，把下面这条结果追加进去：

```json
{
  "role": "tool",
  "tool_call_id": "learn-1",
  "content": "1\thello agent"
}
```

`tool_call_id` 与前面的 `id` 对上。此时第二次发送给模型的是：

```text
system
user：读取 note.txt，告诉我内容
assistant：tool_calls = [read_file，id=learn-1]
tool：tool_call_id=learn-1，content="1\thello agent"
```

模型这次只返回文本，不再申请工具，Agent 将它保存后返回。最终 `agent.messages` 里是四条：

```text
user → assistant(tool_calls) → tool → assistant(最终文本)
```

因此：一次用户输入，正常就可能对应多次模型调用。工具执行之后需要再问模型，它才能根据结果继续处理。

## 6. 失败和拒绝也要回填

`_exec_tool()` 位于 `agent.py:153`，它依次做：

1. 按名称找本实例的工具；找不到就返回错误文本。
2. 用 `inspect.signature(tool.execute).bind(**tc.arguments)` 检查参数是否能绑定到签名。
3. 真正执行工具；普通异常转换为错误文本返回。

签名绑定主要检查缺少或多余参数等调用形状，**不会自动完成 JSON Schema 校验，也不会依据类型注解验证全部参数类型**。

读不到文件，工具返回 `Error: ... not found`；权限不允许写入，`_permit()` 返回 `Permission denied: ...`。它们都仍然进入 `role="tool"` 的结果消息，下一轮模型有机会调整做法。原版没有另一个独立的“异常消息角色”。

`Permission()` 没有交互回调时，会拒绝改动类工具。**完全不传 permission** 则走 `permission=None` 的放行分支，两者不同。

## 7. 边界先记住，后续再深挖

- `max_rounds` 限制外层循环迭代，不是用户消息数、工具总数或真实 HTTP 请求总数。一次迭代可有多个工具；`LLM.chat()` 内部可能重试，上下文压缩也可能额外调用模型。
- 达到上限会直接返回提示字符串，这个提示没有作为最终 assistant 消息追加到历史。
- 多工具的结果按原调用顺序写回历史，但工具在不同线程执行，不能因此假定它们按这个顺序完成或看到前一个操作的结果。
- Ctrl+C 发生在工具执行阶段时，`agent.py:217` 会为缺少结果的调用补 `[interrupted]`，再把中断向上传递。它修复消息配对，不保证已经执行的副作用被撤销。
- `on_reasoning` 处理 provider 输出的独立字段，当前实现只展示，不写进对话历史。理解主循环不需要依赖这个字段。

## 8. 三次离线实验

使用路线文档中的三条 `learning_loop_trace.py` 命令。运行前先预测，运行后核对 `LLM REQUEST 2` 与 `STORED HISTORY`：

| 场景 | 预测工具结果 | 预测循环结果 |
|---|---|---|
| `read` | 带行号的真实文件内容 | 继续第二次模型调用，再返回文本 |
| `missing` | 文件不存在的错误文本 | 仍继续第二次模型调用 |
| `denied` | 权限拒绝文本，文件保持原内容 | 仍继续第二次模型调用 |

脚本复用了仓库的 `ScriptedLLM`，**返回内容预先写好**；它验证的是控制流、真实读文件、权限和消息组装，不验证模型能力、网络流式响应或模型会不会自行纠错。脚本还会检查实际 tool 消息与文件状态，避免被预置的最终回答误导。

建议在 IDE 下依次给 `agent.py:76`、`:90`、`:97`、`:101`、`:84` 加断点。第二轮会先回到 `:76`，再命中 `:84` 返回分支。观察 `self.messages` 长度从 1 → 2 → 3 → 4。

读完本课，先用自己的话回答以下问题，再进入工具系统：

1. 为什么模型返回 `read_file` 还不代表文件已被读取？
2. 如果不把 `tool` 结果追加到历史，下一次模型请求缺少什么？
3. 若响应既有“我先看看文件”又有 `tool_calls`，会在哪一行继续执行？
4. 为什么 `tool_call_id` 必须配对，Ctrl+C 后要修复什么状态？
5. `permission=None` 和 `Permission()` 的运行结果有什么区别？

能解释这五点，再顺着 `Tool.schema()` → `_tool_by_name` → `execute()` 读第二课，会比按目录逐个背文件更容易建立完整理解。
