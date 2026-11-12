# An agent is, at heart, a while loop

If I had only one sentence to explain a coding agent, I'd put it like this: it's a loop that keeps asking the model "what's next," does what the model says, reports the result back to the model, and repeats until the model says "no need to act, I have the answer."

It sounds almost disappointingly plain. But that is the truth of it. Claude Code took this same thing and built it out to hundreds of thousands of lines, yet the most central piece, the part public teardowns call `query.ts`, is at its core a `while` loop of around seventeen hundred lines. CoreCoder writes the same loop and explicit transitions in `corecoder/agent.py` (2620 lines including blanks and comments) and `corecoder/state.py`. The model-tool loop keeps the same shape, while its state and exit reason are now directly inspectable.

In this piece we read that loop closely, section by section.

## The skeleton first

Open `agent.py`. `Agent.chat()` is now the compatibility entry point and the explicit loop lives in `Agent.run()`. This keeps the structure while omitting state and Trace details:

```python
def run(self, task_spec, *, budget=None, cancellation_token=None):
    state = RunState.create(normalise(task_spec))
    budget_controller = RunBudgetController(budget, cancellation_token, self.llm)
    self.messages.append({"role": "user", "content": state.goal})

    while True:
        budget_controller.check("before_model", state, trace_run)
        resp = self.llm.chat(
            messages=self._full_messages(),
            tools=self._tool_schemas(),
            on_token=on_token,
        )

        # no tool calls -> LLM is done, return text
        if not resp.tool_calls:
            self.messages.append(resp.message)
            return AgentResult(state)

        # tool calls -> execute
        self.messages.append(resp.message)
        # ...execute tools, append results back to self.messages...
        self.context.maybe_compress(self.messages, self.llm)

    return "(reached maximum tool-call rounds)"
```

Read these twenty lines and you've read more than half of it. What it does, in order:

Append the user's line to the conversation history. Call for a compression pass (which only acts when the window is nearly full, details in [piece four](04-context_EN.md)). Then enter the loop, and on each round send the full history and every tool's schema to the model. When the model comes back there are only two possibilities. Either it returned a plain block of text with no tool calls, so the run enters `VERIFYING` and caller-configured verifiers inspect external evidence. Or it returned some tool calls, which get executed, with each tool's output appended back to the history as a `tool` message, and then the next round begins, asking the model again.

It turns like that until the model stops asking for tools.

There's a detail here worth pausing on: the model still decides when to stop requesting tools, but it cannot mark the task `VERIFIED`. With no verifier or too little evidence, final text remains `UNVERIFIED`; failed hard conditions such as file state or test results lead to `NEEDS_ACTION` or `FAILED`. The model proposes a conclusion, while the controller decides completion from external evidence. That boundary separates “said it was done” from “demonstrably done.”

## Why for, not while(true)

Claude Code's loop is written as `while(true)`, relying on internal budget and error recovery to exit. CoreCoder now also uses an explicit `while True`, with `RunBudget` checked at every model and tool boundary; without an explicit budget, the legacy `max_rounds` value (50 by default) remains the compatibility limit.

This isn't a style difference, it's a brake. Picture the model stuck in a loop it can't get itself out of: read a file, find it wrong, read again, still wrong, read again. With no cap it would keep burning your tokens until you hit Ctrl+C by hand or the bill makes you wince. The number 50 is empirical; normal tasks come nowhere near it, and if you do hit the cap, the loop returns that very restrained line, `(reached maximum tool-call rounds)`, handing control back to you.

Anyone about to put an LLM in a loop should make giving the loop a hard cap their very first move. It's the cheapest insurance there is.

## What a tool result looks like

The model wants a tool, so we have to feed the execution result back in a format it recognizes. OpenAI's function-calling protocol says that if an `assistant` message carries `tool_calls`, it must be followed by an equal number of `tool` messages, each matching one call by `tool_call_id`. CoreCoder follows that to the letter:

```python
result = self._exec_tool(tc)
self.messages.append({
    "role": "tool",
    "tool_call_id": tc.id,
    "content": result,
})
```

Notice every tool result carries its corresponding `tc.id`. This id-pairing relationship is the root of several traps to come. Remember it; we'll return to this constraint when piece four covers context compression and when this piece covers interruption below.

## How a single tool call tells "bad arguments" apart from "the tool blew up"

`_exec_tool` is a dozen-odd lines, but it hides an engineering judgment I'm fond of:

```python
def _exec_tool(self, tc) -> str:
    tool = self._tool_by_name.get(tc.name)
    if tool is None:
        return f"Error: unknown tool '{tc.name}'"
    # validate arguments first so a TypeError raised *inside* the tool isn't
    # mislabelled as a bad-arguments error from the caller
    try:
        inspect.signature(tool.execute).bind(**tc.arguments)
    except TypeError as e:
        return f"Error: bad arguments for {tc.name}: {e}"
    try:
        return tool.execute(**tc.arguments)
    except Exception as e:
        return f"Error executing {tc.name}: {e}"
```

Why try the arguments once with `inspect.signature().bind()` before actually calling? Because if you go straight to `tool.execute(**tc.arguments)` and a `TypeError` is raised, you can't tell whether the model's arguments didn't match the function signature (the model's fault, it should fix them) or some line inside the tool itself threw a `TypeError` (the tool's bug, nothing to do with the arguments). The feedback returned to the model should be completely different for these two: the former says "you filled the arguments in wrong," the latter says "the tool failed while running."

Binding first pulls the question of "do the arguments fit the signature" out on its own. `bind` doesn't actually run the function; it only checks whether the actual arguments can legally bind to the parameters. If they can't bind, it's an argument problem. If they bind and then it blows up during the real call, that's a problem inside the tool. This distinction is written into the test `test_exec_tool_distinguishes_bad_args_from_internal_error`: a tool that throws a `TypeError` internally is not misreported as an argument error.

This kind of detail is the distance between "a demo that runs" and "a tool that doesn't trip you up." Hand the model a precise error and it self-corrects on the next round; hand it a misleading one and it drifts further the wrong way.

## Each agent only knows its own set of tools

Notice the line above uses `self._tool_by_name`, built in the constructor:

```python
self._tool_by_name = {t.name: t for t in self.tools}
```

It's an instance-level dictionary, not a global table. On the main agent this makes no visible difference, but for [sub-agents](05-parallel-and-subagents_EN.md) it matters: when the main agent spawns a sub-agent, it trims part of the tool set (for example, it won't let the sub-agent spawn a grand-sub-agent). If tool lookup went through a global table, that trim would be toothless and the sub-agent could still call the forbidden tool. The instance-level dictionary guarantees that "which tools this agent can use" is its own business, and nobody can step over it. The test `test_agent_tool_scope_is_per_instance` watches exactly this: an agent given only `read_file`, when it calls `bash`, gets `unknown tool 'bash'`, even though `bash` is a genuinely registered tool.

## The instant the model gets interrupted

In real use, a user hits Ctrl+C at any moment. The trouble is that Ctrl+C might land right in the middle, after the model has returned a batch of tool calls but before they've all finished running. At that point the history has an assistant message carrying `tool_calls` but missing some of the matching `tool` replies. Send that broken history out on the next request and an OpenAI-compatible API rejects it outright, because it violates the "every tool_call must have a paired reply" protocol. One interruption, and the whole session is poisoned.

CoreCoder handles this by catching the exception inside the loop on purpose:

```python
except KeyboardInterrupt:
    # Ctrl+C mid-execution would leave the assistant tool_calls
    # message without replies, poisoning the next request; backfill
    self._answer_pending_tool_calls(resp.tool_calls)
    raise
```

`_answer_pending_tool_calls` does something simple: it backfills an `[interrupted]` placeholder reply for every tool call that hasn't gotten one yet, makes the history legal again, and then re-raises the exception for the upper layer to handle:

```python
def _answer_pending_tool_calls(self, tool_calls):
    answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
    for tc in tool_calls:
        if tc.id not in answered:
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": "[interrupted]",
            })
```

It first collects the ids already answered and only fills placeholders for the missing ones, so results from tools that already finished aren't overwritten (the test `test_interrupt_backfills_missing_tool_replies` verifies precisely this: an already-answered call doesn't get filled again). This way the user can keep chatting after an interruption, with a clean history.

This code isn't complicated, but it represents an important kind of engineering thinking: your loop must handle not only "ran to completion normally" but also "got cut off at an arbitrary step." The gap between an agent that runs and an agent you can ship is often exactly in how these half-finished states get cleaned up.

## Compared with Claude Code

Put CoreCoder's 150 lines next to Claude Code's `query.ts` and you'll find the loop's skeleton almost overlaps: assemble messages, call the model with tools, execute when the model wants tools, backfill the results, loop again, and finish when the model returns text. This structure isn't something CoreCoder copied; it's the shared paradigm of this generation of coding agents, and anyone writing one ends up here.

The real difference is the ring of protection around the loop. Claude Code's loop is wrapped in a far thicker layer of error recovery: back off and retry on rate limits, auto-compress and retry when context overflows, switch to a fallback model on a server-side 529, retry a few times when output gets truncated, plus finer budget control. CoreCoder splits these out elsewhere: retry lives in [`llm.py`](03-llm-and-cost_EN.md), compression in [`context.py`](04-context_EN.md), and round, tool-call, token, known-price cost, deadline, and no-progress limits live together in `budget.py`. Same shape, different thickness.

## Wrapping up

Compressed to one sentence: an agent's core is a capped loop that asks the model, runs tools, backfills results, and asks again, until the model returns only text. But what really decides whether it's good to use are the unremarkable bits of handling along the edges of that loop. The model decides on its own when to stop, so its behavior is inherently less controllable than a traditional program. Tool calls and replies must pair strictly by id, and that constraint keeps manufacturing traps during interruption and compression. As for telling "bad arguments" apart from "the tool blew up," capping the loop, and backfilling placeholders on interruption, those are the real distance between a demo and something deliverable.

Next piece, we look at what actually lets this loop act: the tool system.
