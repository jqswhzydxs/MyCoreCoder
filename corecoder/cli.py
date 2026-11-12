"""cli.py
Interactive REPL - the user-facing terminal interface."""

import argparse
import os
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .agent import Agent
from .budget import CancellationToken
from .config import Config
from .events import AgentEvent, AgentEventType
from .hooks import load_hooks
from .llm import LLM, LiteLLM
from .mcp import load_mcp_tools
from .permissions import DEFAULT_STORE, Permission, PermissionRequestBroker
from .run_store import DEFAULT_RUN_STORE, RunStore, RunStoreError
from .runtime import AgentRunHandle, SteeringMode
from .session import list_sessions, load_session, save_session
from .tools import ALL_TOOLS
from .trace import JsonlTraceSink

console = Console()
err_console = Console(stderr=True)


def _parse_args():
    p = argparse.ArgumentParser(
        prog="corecoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $CORECODER_MODEL or gpt-5.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("--yes", action="store_true", help="Auto-approve every tool call (for scripts and CI)")
    p.add_argument("--demo", action="store_true", help="Run the offline scripted demo (no API key needed)")
    p.add_argument("--trace-file", metavar="PATH", help="Append redacted runtime events to a JSONL file")
    resume_group = p.add_mutually_exclusive_group()
    resume_group.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    resume_group.add_argument(
        "--resume-run",
        metavar="RUN_ID",
        help="Safely resume a task from the durable run store",
    )
    p.add_argument(
        "--run-store",
        metavar="PATH",
        help=f"SQLite run store (default: {DEFAULT_RUN_STORE})",
    )
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args()


def main():
    args = _parse_args()
    trace_sink = JsonlTraceSink(args.trace_file) if args.trace_file else None

    if args.demo:
        from .demo import run_demo
        raise SystemExit(run_demo(trace_sink=trace_sink))

    config = Config.from_env()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    try:
        run_store = RunStore(args.run_store or DEFAULT_RUN_STORE)
    except RunStoreError as exc:
        console.print(f"[red]Run store unavailable:[/] {exc}")
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    # consent layer: ask in the REPL, refuse in one-shot mode, --yes skips it
    if args.yes:
        permission = Permission(allow_all=True)
    elif args.prompt or args.resume_run:
        permission = Permission()
    else:
        permission = Permission(
            broker=PermissionRequestBroker(),
            persist_path=DEFAULT_STORE,
        )
    agent = Agent(
        llm=llm,
        tools=[*ALL_TOOLS, *load_mcp_tools()],
        max_context_tokens=config.max_context_tokens,
        permission=permission,
        hooks=load_hooks(),
        trace_sink=trace_sink,
        run_store=run_store,
    )

    # resume saved session
    if args.resume:
        loaded = load_session(args.resume)
        if loaded:
            agent.messages, loaded_model = loaded
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    if args.resume_run:
        if args.prompt:
            console.print("[red]--resume-run cannot be combined with --prompt.[/red]")
            sys.exit(2)
        try:
            result = agent.resume_run(args.resume_run)
        except RunStoreError as exc:
            console.print(f"[red]Unable to resume run:[/] {exc}")
            sys.exit(1)
        console.print(Markdown(result.display_text))
        console.print(
            f"[dim]Run {result.run_id}: {result.status.value} "
            f"({result.exit_reason.value})[/dim]"
        )
        return

    # one-shot mode
    if args.prompt:
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config)


def _ask_permission(tool_name: str, arguments: dict) -> str:
    """REPL consent prompt. Anything but a clear yes counts as a no."""
    console.print(f"\n[bold yellow]permission requested:[/] [cyan]{tool_name}[/cyan]({_brief(arguments)})")
    try:
        answer = pt_prompt("  [y] allow once  [a] always allow this tool  [n] deny: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]denied[/dim]")
        return "deny"
    if answer in ("y", "yes"):
        return "once"
    if answer in ("a", "always"):
        return "always"
    return "deny"


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    perm = agent.permission
    if perm is not None and perm.ask is None and not perm.allow_all:
        console.print("[dim]one-shot mode: mutating tools are refused unless you pass --yes[/dim]")

    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    def on_reasoning(tok):
        # thinking goes to stderr so piped stdout stays the plain answer
        err_console.print(f"[dim]{tok}[/dim]", end="")

    cancellation_token = CancellationToken()
    try:
        agent.chat(
            prompt,
            on_token=on_token,
            on_tool=on_tool,
            on_reasoning=on_reasoning,
            cancellation_token=cancellation_token,
        )
    except KeyboardInterrupt:
        cancellation_token.cancel("Keyboard interrupt requested by the CLI user.")
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        # one-shot mode: print whatever went wrong and exit non-zero
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    print()


@dataclass(slots=True)
class _InteractiveRun:
    handle: AgentRunHandle
    renderer: "CLIEventRenderer"


class CLIEventRenderer:
    """Render one ordered Agent event stream without reading terminal input."""

    def __init__(self, handle: AgentRunHandle, *, on_done=None) -> None:
        self.handle = handle
        self.on_done = on_done
        self.done = threading.Event()
        self.sequences: list[int] = []
        self._started = time.monotonic()
        self._tool_started: dict[str, float] = {}
        self._assistant_parts: list[str] = []
        self._token_line_open = False
        self._tool_output_call: str | None = None
        self._tool_output_channel: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"corecoder-render-{handle.run_id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            for event in self.handle.events():
                if self.sequences and event.sequence <= self.sequences[-1]:
                    continue
                self.sequences.append(event.sequence)
                self._render(event)
            self._render_final()
        finally:
            self._close_token_line()
            self._close_tool_output_line()
            self.done.set()
            if self.on_done is not None:
                try:
                    self.on_done()
                except Exception:  # noqa: BLE001 - prompt refresh is best effort
                    self.on_done = None

    def _render(self, event: AgentEvent) -> None:
        event_type = event.event_type
        data = event.public_data
        if event_type is AgentEventType.ASSISTANT_TOKEN:
            self._close_tool_output_line()
            self._assistant_parts.append(event.message)
            print(event.message, end="", flush=True)
            self._token_line_open = True
            return
        if event_type is AgentEventType.RUN_STARTED:
            self._line("开始分析任务")
        elif event_type is AgentEventType.MODEL_STARTED:
            self._line("模型开始处理")
        elif event_type is AgentEventType.MODEL_STREAM_ACTIVITY:
            activity = data.get("activity")
            self._line(
                "模型正在准备工具调用"
                if activity == "tool_call_detected"
                else "模型正在分析"
            )
        elif event_type is AgentEventType.TOOL_STARTED:
            call_id = str(data.get("tool_call_id", ""))
            self._tool_started[call_id] = time.monotonic()
            self._line(f"▶ {data.get('tool_name', 'tool')}")
        elif event_type is AgentEventType.TOOL_OUTPUT:
            self._render_tool_output(data)
        elif event_type is AgentEventType.CACHE_HIT:
            self._close_tool_output_line()
            self._line(f"⚡ {data.get('tool_name', 'tool')}（缓存命中）")
        elif event_type in {AgentEventType.TOOL_FINISHED, AgentEventType.TOOL_FAILED}:
            call_id = str(data.get("tool_call_id", ""))
            started = self._tool_started.pop(call_id, None)
            suffix = f"（{time.monotonic() - started:.1f}s）" if started is not None else ""
            marker = "✓" if event_type is AgentEventType.TOOL_FINISHED else "✗"
            outcome = "完成" if event_type is AgentEventType.TOOL_FINISHED else "未完成"
            self._line(f"{marker} {data.get('tool_name', 'tool')} {outcome}{suffix}")
        elif event_type is AgentEventType.STEERING_RECEIVED:
            self._line("补充指令已收到，等待安全点")
        elif event_type is AgentEventType.STEERING_APPLIED:
            self._line("补充指令已应用")
        elif event_type is AgentEventType.REPLANNING:
            skipped = data.get("skipped_tool_call_count")
            self._line(
                f"已跳过 {skipped} 个未执行操作，正在重新规划"
                if isinstance(skipped, int) and skipped
                else "正在根据新要求重新规划"
            )
        elif event_type is AgentEventType.PAUSE_REQUESTED:
            self._line("暂停请求已收到，等待安全点")
        elif event_type is AgentEventType.RUN_PAUSED:
            self._line("运行已暂停")
        elif event_type is AgentEventType.RUN_RESUMED:
            self._line("运行已继续")
        elif event_type is AgentEventType.CANCELLATION_REQUESTED:
            self._line("取消请求已收到，等待安全停止")
        elif event_type is AgentEventType.VERIFICATION_STARTED:
            self._line("开始验证结果")
        elif event_type is AgentEventType.VERIFICATION_FINISHED:
            self._line("结果验证完成")
        elif event_type is AgentEventType.RUN_CANCELLED:
            self._line("运行已取消")
        elif event_type is AgentEventType.RUN_FAILED:
            self._line("运行失败")
        elif event_type is AgentEventType.RUN_FINISHED:
            self._line("运行完成")

    def _render_final(self) -> None:
        try:
            result = self.handle.result()
        except BaseException as exc:  # noqa: BLE001 - keep the REPL alive
            self._line(f"错误：{exc}")
            return
        streamed = "".join(self._assistant_parts)
        if result.final_text and not streamed.endswith(result.final_text):
            self._close_token_line()
            self._close_tool_output_line()
            console.print(Markdown(result.display_text))

    def _line(self, text: str) -> None:
        self._close_token_line()
        self._close_tool_output_line()
        elapsed = max(0, int(time.monotonic() - self._started))
        console.print(f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {text}")

    def _render_tool_output(self, data: dict) -> None:
        call_id = str(data.get("tool_call_id", ""))
        channel = str(data.get("channel", "stdout"))
        text = str(data.get("text", ""))
        if not text:
            return
        if self._tool_output_call != call_id:
            self._close_tool_output_line()
            self._tool_output_call = call_id
            self._tool_output_channel = None
        if self._tool_output_channel != channel:
            self._close_token_line()
            if self._tool_output_channel is not None:
                print(flush=True)
            self._tool_output_channel = channel
            if channel == "stderr":
                print("│ stderr: ", end="", flush=True)
            else:
                print("│ ", end="", flush=True)
        rendered = text.replace("\n", "\n│ ")
        print(rendered, end="", flush=True)

    def _close_tool_output_line(self) -> None:
        if self._tool_output_call is not None:
            print(flush=True)
            self._tool_output_call = None
            self._tool_output_channel = None

    def _close_token_line(self) -> None:
        if self._token_line_open:
            print(flush=True)
            self._token_line_open = False


def _invalidate_prompt(session) -> None:
    try:
        session.app.invalidate()
    except Exception:  # noqa: BLE001 - fake/inactive sessions need no refresh
        return


def _repl(
    agent: Agent,
    config: Config,
    *,
    prompt_session=None,
    patch_output: bool = True,
):
    """Interactive read-eval-print loop."""
    perm = agent.permission
    mode = "auto-approve every tool call (--yes)" if (perm and perm.allow_all) else "ask before mutating tools"
    mcp_count = sum(1 for t in agent.tools if t.name.startswith("mcp__"))
    console.print(Panel(
        f"[bold]CoreCoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + f"\nPermissions: [cyan]{mode}[/cyan]"
        + (f"\nHooks: [cyan]{len(agent.hooks.pre)} pre, {len(agent.hooks.post)} post[/cyan]"
           " from ~/.corecoder/hooks.json" if agent.hooks else "")
        + (f"\nMCP: [cyan]{mcp_count} tools[/cyan] from ~/.corecoder/mcp.json" if mcp_count else "")
        + f"\nTrace: [cyan]{agent.trace.status()}[/cyan]"
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))

    hist_path = os.path.expanduser("~/.corecoder_history")
    history = FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    session = prompt_session or PromptSession(
        history=history,
        multiline=True,
        key_bindings=kb,
        prompt_continuation="...  ",
    )
    broker = None
    if perm is not None and not perm.allow_all:
        broker = perm.broker
        if broker is None:
            broker = PermissionRequestBroker()
            perm.broker = broker
            perm.ask = None
        broker.set_wakeup(lambda: _invalidate_prompt(session))
    active: _InteractiveRun | None = None

    while True:
        if active is not None and active.handle.is_done:
            active.renderer.join(timeout=1)
            active = None

        def prompt_text(current_active=active) -> str:
            request = broker.peek() if broker is not None else None
            if request is not None:
                return f"Permission {request.tool_name} [y/a/n] > "
            if current_active is not None and not current_active.handle.is_done:
                return "Steer > "
            return "You (plan) > " if agent.plan_mode else "You > "

        try:
            output_context = patch_stdout(raw=True) if patch_output else nullcontext()
            with output_context:
                user_input = session.prompt(prompt_text).strip()
        except EOFError:
            if active is not None and not active.handle.is_done:
                active.handle.cancel("EOF received by the interactive CLI.")
                if broker is not None:
                    broker.deny_all()
                active.handle.close()
            console.print("\nBye!")
            break
        except KeyboardInterrupt:
            if active is not None and not active.handle.is_done:
                active.handle.cancel("Keyboard interrupt requested by the CLI user.")
                if broker is not None:
                    broker.deny_all()
                console.print("\n[yellow]Cancellation requested.[/yellow]")
                continue
            console.print("\nBye!")
            break

        if not user_input:
            continue

        if active is not None and active.handle.is_done:
            active.renderer.join(timeout=1)
            active = None

        permission_request = broker.peek() if broker is not None else None
        if permission_request is not None:
            if (
                active is not None
                and user_input.startswith("/")
                and _handle_running_input(user_input, active, broker)
            ):
                continue
            answer = user_input.strip().lower()
            verdict = (
                "once" if answer in {"y", "yes"}
                else "always" if answer in {"a", "always"}
                else "deny"
            )
            broker.resolve(permission_request.request_id, verdict)
            console.print(f"[dim]permission: {verdict}[/dim]")
            continue

        if active is not None and not active.handle.is_done:
            _handle_running_input(user_input, active, broker)
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/plan":
            agent.plan_mode = not agent.plan_mode
            if agent.plan_mode:
                console.print(
                    "[yellow]Plan mode on.[/yellow] The agent can look but not touch: it will "
                    "investigate read-only and present a plan. Type [bold]approve[/bold] to "
                    "accept the plan, or [bold]/plan[/bold] again to exit."
                )
            else:
                console.print("[yellow]Plan mode off.[/yellow]")
            continue
        if agent.plan_mode and user_input.lower() in ("approve", "/approve"):
            agent.plan_mode = False
            console.print("[yellow]Plan mode off.[/yellow]")
            user_input = "approve"  # the approval itself goes to the model, which then executes
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/trace":
            console.print(f"Trace: [cyan]{agent.trace.status()}[/cyan]")
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            compressed = agent.context.maybe_compress(agent.messages, agent.llm)
            after = estimate_tokens(agent.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            continue
        if user_input == "/save":
            sid = save_session(agent.messages, config.model)
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: corecoder -r {sid}")
            continue
        if user_input == "/diff":
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/undo":
            from .checkpoints import pending, undo
            console.print(undo())
            left = pending()
            if left:
                console.print(f"[dim]{left} more checkpoint(s) on the stack.[/dim]")
            continue
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")
            continue
        if user_input == "/runs":
            _show_runs(agent)
            continue
        if user_input == "/run-status" or user_input.startswith("/run-status "):
            run_id = user_input[len("/run-status"):].strip() or agent.last_run_id
            _show_run_status(agent, run_id)
            continue
        if user_input == "/mcp" or user_input.startswith("/mcp "):
            _mcp(user_input[4:].strip())
            continue

        # an unknown /command shouldn't be sent to the model as a prompt
        if user_input.startswith("/"):
            console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
            continue

        # Ordinary interactive tasks run in the existing background runtime.
        try:
            handle = agent.start(user_input)
            renderer = CLIEventRenderer(
                handle,
                on_done=lambda: _invalidate_prompt(session),
            )
            active = _InteractiveRun(handle, renderer)
            renderer.start()
        except Exception as e:  # noqa: BLE001
            console.print(f"\n[red]Error: {e}[/red]")

    if broker is not None:
        broker.close()


def _handle_running_input(
    user_input: str,
    active: _InteractiveRun,
    broker: PermissionRequestBroker | None,
) -> bool:
    """Route one ``Steer >`` input without touching Agent-owned mutable state."""
    handle = active.handle
    try:
        if user_input == "/pause":
            handle.pause()
            return True
        if user_input == "/resume":
            handle.resume()
            return True
        if user_input == "/cancel":
            handle.cancel("Cancellation requested from the interactive CLI.")
            if broker is not None:
                broker.deny_all()
            return True
        if user_input == "/status":
            _show_live_status(handle, broker)
            return True
        if user_input == "/help":
            _show_help()
            return True
        if user_input == "/tokens":
            console.print("[dim]Token totals are available again after the active run.[/dim]")
            return True
        if user_input == "/trace":
            console.print("[dim]Trace status is unchanged during the active run.[/dim]")
            return True
        if user_input == "/steer" or user_input.startswith("/steer "):
            text = user_input[len("/steer"):].strip()
            if not text:
                console.print("[yellow]Usage: /steer <text>[/yellow]")
                return True
            if broker is not None:
                broker.deny_all()
            handle.steer(text, mode=SteeringMode.REDIRECT)
            return True
        if user_input == "/append" or user_input.startswith("/append "):
            text = user_input[len("/append"):].strip()
            if not text:
                console.print("[yellow]Usage: /append <text>[/yellow]")
                return True
            handle.steer(text, mode=SteeringMode.APPEND)
            return True
        if user_input.lower() in {"quit", "exit", "/quit", "/exit"}:
            console.print("[yellow]A run is active; use /cancel before exiting.[/yellow]")
            return True
        if user_input.startswith("/"):
            console.print(
                "[yellow]That command is unavailable while a run is active; "
                "use /status, /steer, /append, /pause, /resume, or /cancel.[/yellow]"
            )
            return True
        if broker is not None:
            broker.deny_all()
        handle.steer(user_input, mode=SteeringMode.REDIRECT)
        return True
    except (RuntimeError, TypeError, ValueError) as exc:
        console.print(f"[yellow]Control rejected:[/] {exc}")
        return True


def _show_live_status(
    handle: AgentRunHandle,
    broker: PermissionRequestBroker | None,
) -> None:
    status = handle.status_snapshot()
    waiting_permission = bool(broker and broker.has_pending)
    console.print(
        f"Run [cyan]{status['run_id']}[/cyan]: [bold]{status['status']}[/bold]\n"
        f"Step: {status['current_step']}  Round: {status['round']}\n"
        f"Executed tools: {status['executed_tool_count']}  "
        f"Elapsed: {status['elapsed_seconds']:.1f}s\n"
        f"Pending steering: {status['pending_steering_count']}  "
        f"Waiting permission: {'yes' if waiting_permission else 'no'}"
    )


def _mcp(arg: str):
    """The /mcp command: server states, or `reconnect <name>` to revive one."""
    from . import mcp
    parts = arg.split()
    if not parts:
        if not mcp._live_clients:
            console.print("[dim]No MCP servers running (configure ~/.corecoder/mcp.json).[/dim]")
        for client in mcp._live_clients:
            if client.error is None:
                console.print(f"  [cyan]{client.name}[/cyan]: alive, {len(client.tools)} tools")
            else:
                console.print(
                    f"  [cyan]{client.name}[/cyan]: [red]dead[/red] ({client.error})"
                    f", revive with /mcp reconnect {client.name}"
                )
        return
    if len(parts) == 2 and parts[0] == "reconnect":
        client = next((c for c in mcp._live_clients if c.name == parts[1]), None)
        if client is None:
            console.print(f"[red]No MCP server named '{parts[1]}'.[/red]")
            return
        try:
            client.reconnect()
        except (mcp.MCPError, OSError) as e:
            console.print(f"[red]Reconnect failed:[/red] {e}")
        else:
            console.print(f"[green]Reconnected:[/green] [cyan]{client.name}[/cyan], {len(client.tools)} tools")
        return
    console.print("[yellow]Usage: /mcp [reconnect <name>][/yellow]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /trace         Show runtime trace status\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /undo          Revert the most recent file change\n"
        "  /plan          Toggle plan mode: read-only, then a plan to approve\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  /runs          List durable task runs\n"
        "  /run-status    Show the latest durable task status\n"
        "  /run-status ID Show one durable task status\n"
        "  /mcp           MCP server status; /mcp reconnect <name> revives a dead one\n"
        "  /steer TEXT    Redirect an active run at its next safe point\n"
        "  /append TEXT   Append guidance without cancelling planned safe work\n"
        "  /pause         Pause an active run at its next safe point\n"
        "  /resume        Resume a paused run\n"
        "  /cancel        Request cooperative cancellation\n"
        "  /status        Show live active-run status\n"
        "  quit           Exit CoreCoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="CoreCoder Help",
        border_style="dim",
    ))


def _show_runs(agent: Agent) -> None:
    if agent.run_store is None:
        console.print("[dim]Durable run storage is disabled.[/dim]")
        return
    try:
        runs = agent.run_store.list_runs()
    except RunStoreError as exc:
        console.print(f"[red]Run store unavailable:[/] {exc}")
        return
    if not runs:
        console.print("[dim]No durable runs.[/dim]")
        return
    for item in runs:
        console.print(
            f"  [cyan]{item['run_id']}[/cyan] {item['status']} "
            f"round={item['round']} updated={item['updated_at']} "
            f"{item['goal']}"
        )


def _show_run_status(agent: Agent, run_id: str | None) -> None:
    if agent.run_store is None:
        console.print("[dim]Durable run storage is disabled.[/dim]")
        return
    if not run_id:
        console.print("[dim]No current run. Use /run-status RUN_ID.[/dim]")
        return
    try:
        status = agent.run_store.run_status(run_id)
    except RunStoreError as exc:
        console.print(f"[red]Run status unavailable:[/] {exc}")
        return
    calls = ", ".join(
        f"{name}={count}" for name, count in status["tool_calls"].items() if count
    ) or "none"
    console.print(
        f"Run [cyan]{status['run_id']}[/cyan]: [bold]{status['status']}[/bold]\n"
        f"Step: {status['current_step']}  Round: {status['round']}  "
        f"Version: {status['state_version']}\n"
        f"Exit: {status['exit_reason'] or '-'}  Tool calls: {calls}\n"
        f"Updated: {status['updated_at']}"
    )
    for call in status["recovery_required"]:
        console.print(
            f"  [yellow]manual check required:[/] {call['tool_call_id']} "
            f"({call['tool_name']}) — {call['recovery_note'] or 'outcome unknown'}"
        )


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")
