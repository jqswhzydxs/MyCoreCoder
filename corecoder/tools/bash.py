"""Shell command execution with safety checks.

Claude Code's BashTool is 1,143 lines. This is the distilled version:
- Incremental stdout/stderr streaming with head+tail truncation
- Timeout and cooperative cancellation, including process-tree cleanup
- Dangerous command detection
- Working directory tracking (cd awareness)
"""

import codecs
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import ClassVar

from ..shell import spawn_shell
from .base import Tool
from .protocol import ToolError, ToolExecutionContext, ToolOutcome, ToolResult

# Track cwd across commands (Claude Code does this too). Thread-local, so that
# when the agent executes tools in parallel two bash calls never race on one
# shared global: each worker thread carries its own cwd. See article 05.
_local = threading.local()


def get_tracked_cwd() -> str | None:
    """The cwd tracked for this thread, if any."""
    return getattr(_local, "cwd", None)


def set_tracked_cwd(path: str | None) -> None:
    """Set the tracked cwd for this thread (session handoff at pool edges)."""
    if path is None:
        if hasattr(_local, "cwd"):
            del _local.cwd
    else:
        _local.cwd = path

# patterns that could wreck the filesystem or leak secrets
_DANGEROUS_PATTERNS = [
    # recursive delete aimed at root/home (force flag optional)
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "recursive delete on home/root"),
    # recursive (-r/-R) and force (-f) flags together, in any order or spacing
    (r"\brm\b(?=(?:.*\s)?-\w*[rR])(?=(?:.*\s)?-\w*f)", "force recursive delete"),
    # the same, written with long-form flags
    (r"\brm\b.*--recursive\b.*--force\b|\brm\b.*--force\b.*--recursive\b", "force recursive delete"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*of=/dev/", "raw disk write"),
    (r">\s*/dev/sd[a-z]", "overwrite block device"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "chmod 777 on root"),
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?(ba)?sh\b", "pipe curl to shell"),
    (r"\bwget\b.*\|\s*(sudo\s+)?(ba)?sh\b", "pipe wget to shell"),
]


_MAX_CAPTURE_CHARS = 2_000_000
_OUTPUT_HEAD = 6_000
_OUTPUT_TAIL = 3_000
_MAX_TOOL_OUTPUT_CHARS = 15_000


class _OutputCapture:
    """Thread-safe command output buffer with a hard memory ceiling."""

    def __init__(self, limit: int = _MAX_CAPTURE_CHARS) -> None:
        self._limit = limit
        self._parts: list[str] = []
        self._length = 0
        self._omitted = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            remaining = self._limit - self._length
            if remaining <= 0:
                self._omitted += len(text)
                return
            chunk = text[:remaining]
            self._parts.append(chunk)
            self._length += len(chunk)
            self._omitted += len(text) - len(chunk)

    def value(self) -> str:
        with self._lock:
            text = "".join(self._parts)
            omitted = self._omitted
        if omitted:
            text += f"\n... {omitted} chars omitted from captured output ...\n"
        return text


class _OutputRelay:
    """Forward chunks to an observer without letting a slow sink block pipes.

    Pipe readers must always drain, otherwise a chatty child can fill the OS
    pipe and deadlock while the runtime is still waiting for output.  This
    relay therefore uses a small bounded queue and drops *observation* chunks
    when the consumer cannot keep up.  Captured model output is kept separately
    and is never affected by this policy.
    """

    MAX_CHARS = 1_000_000
    MAX_EVENTS = 2_000

    def __init__(self, sink, max_queue: int = 64) -> None:
        self._sink = sink
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._closed = threading.Event()
        self._inflight = threading.Event()
        self._lock = threading.Lock()
        self._chars = 0
        self._events = 0
        self.dropped = 0
        self._worker = threading.Thread(
            target=self._run,
            name="corecoder-tool-output",
            daemon=True,
        )

    def start(self) -> None:
        self._worker.start()

    def publish(self, channel: str, text: str) -> None:
        if not text or self._closed.is_set():
            return
        with self._lock:
            if self._chars + len(text) > self.MAX_CHARS or self._events >= self.MAX_EVENTS:
                self.dropped += 1
                return
            self._chars += len(text)
            self._events += 1
        try:
            self._queue.put_nowait((channel, text))
        except queue.Full:
            with self._lock:
                self.dropped += 1

    def close(self) -> None:
        if self._closed.is_set():
            return
        # Let already-published chunks reach the emitter so their sequence
        # numbers precede TOOL_FINISHED.  If the observer is too slow, drop
        # the backlog rather than delaying the tool result indefinitely.
        deadline = time.monotonic() + 2.0
        while (
            (not self._queue.empty() or self._inflight.is_set())
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self._closed.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._worker.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            try:
                channel, text = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._closed.is_set():
                    return
                continue
            # A chunk fetched before close() must still be delivered: the
            # terminal event is emitted only after this worker is joined.
            self._inflight.set()
            try:
                self._sink(channel, text)
            except Exception:  # noqa: BLE001,S110 - observation is best effort
                pass
            finally:
                self._inflight.clear()


@dataclass(slots=True)
class _CommandOutcome:
    text: str
    returncode: int | None
    timed_out: bool = False
    cancelled: bool = False


def _format_duration(seconds: float) -> str:
    return f"{seconds:g}"


def _format_command_output(
    stdout: str,
    stderr: str,
    returncode: int | None,
    *,
    timed_out: bool,
    cancelled: bool,
    timeout: float,
) -> str:
    out = stdout
    if stderr:
        out += f"\n[stderr]\n{stderr}"
    if returncode not in (None, 0):
        out += f"\n[exit code: {returncode}]"
    if cancelled:
        out += "\n\nError: command cancelled."
    elif timed_out:
        out += f"\n\nError: timed out after {_format_duration(timeout)}s"

    if len(out) > _MAX_TOOL_OUTPUT_CHARS:
        out = (
            out[:_OUTPUT_HEAD]
            + f"\n\n... truncated ({len(out)} chars total) ...\n\n"
            + out[-_OUTPUT_TAIL:]
        )
    return out.strip() or "(no output)"


def _pump_stream(stream, channel: str, capture: _OutputCapture, relay: _OutputRelay | None) -> None:
    """Decode one pipe incrementally and publish each chunk as it arrives."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    read = getattr(stream, "read1", None) or stream.read
    try:
        while True:
            try:
                data = read(4096)
            except (OSError, ValueError):
                break
            if not data:
                break
            if isinstance(data, bytes):
                text = decoder.decode(data)
            else:
                text = str(data)
            if not text:
                continue
            capture.append(text)
            if relay is not None:
                relay.publish(channel, text)
        tail = decoder.decode(b"", final=True)
        if tail:
            capture.append(tail)
            if relay is not None:
                relay.publish(channel, tail)
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001,S110 - best effort cleanup
            pass


def _taskkill_tree(pid: int, *, force: bool = True) -> None:
    """Best-effort Windows process-tree termination."""
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    try:
        subprocess.run(
            args,
            timeout=5,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _terminate_process(proc: subprocess.Popen) -> None:
    """Terminate a running child, escalating to a tree kill after grace."""
    if proc.poll() is not None:
        return

    if os.name == "nt":
        # The shell may have spawned the actual workload.  terminate() on the
        # shell alone leaves grandchildren holding our stdout/stderr pipes
        # open, so ask taskkill for the whole tree.  Windows has no portable
        # SIGTERM-for-process-group equivalent; this is the reliable stop.
        _taskkill_tree(proc.pid, force=True)
        try:
            proc.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _close_streams(proc: subprocess.Popen, readers: list[threading.Thread], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001,S110 - cleanup must not mask result
                pass
    for reader in readers:
        if reader.is_alive():
            reader.join(timeout=0.5)


def _run_streaming(
    command: str,
    timeout: float,
    cwd: str,
    context: ToolExecutionContext | None,
) -> _CommandOutcome:
    """Run one command with concurrent pipe drains and cooperative control."""
    limit = max(0.0, float(timeout))
    if context is not None:
        remaining = context.remaining_timeout()
        if remaining is not None:
            limit = min(limit, max(0.0, remaining))
    if context is not None and context.cancellation_probe():
        return _CommandOutcome(
            "Error: command cancelled.",
            None,
            cancelled=True,
        )
    if limit <= 0:
        return _CommandOutcome(
            f"Error: timed out after {_format_duration(limit)}s",
            None,
            timed_out=True,
        )

    stdout_capture = _OutputCapture()
    stderr_capture = _OutputCapture()
    relay = _OutputRelay(context.emit_output) if context is not None else None
    if relay is not None:
        relay.start()

    try:
        env = os.environ.copy()
        # Windows Python children otherwise write the locale code page into a
        # pipe.  CoreCoder consumes UTF-8, so keep nested Python deterministic.
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        proc = spawn_shell(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
        )
    except Exception:
        if relay is not None:
            relay.close()
        raise

    readers = [
        threading.Thread(
            target=_pump_stream,
            args=(proc.stdout, "stdout", stdout_capture, relay),
            name="corecoder-bash-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_pump_stream,
            args=(proc.stderr, "stderr", stderr_capture, relay),
            name="corecoder-bash-stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + limit
    stop_reason: str | None = None
    while True:
        if proc.poll() is not None:
            break
        if context is not None and context.cancellation_probe():
            stop_reason = "cancelled"
            break
        if time.monotonic() >= deadline:
            stop_reason = "timeout"
            break
        time.sleep(0.02)

    if stop_reason is not None:
        _terminate_process(proc)

    try:
        returncode = proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        _terminate_process(proc)
        try:
            returncode = proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            returncode = proc.poll()

    _close_streams(proc, readers)
    if relay is not None:
        relay.close()

    text = _format_command_output(
        stdout_capture.value(),
        stderr_capture.value(),
        returncode,
        timed_out=stop_reason == "timeout",
        cancelled=stop_reason == "cancelled",
        timeout=limit,
    )
    return _CommandOutcome(
        text,
        returncode,
        timed_out=stop_reason == "timeout",
        cancelled=stop_reason == "cancelled",
    )


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command. Returns stdout, stderr, and exit code. "
        "Use this for running tests, installing packages, git operations, etc."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120)",
            },
        },
        "required": ["command"],
    }
    side_effect = "external"
    risk = "high"
    required_permissions = ("shell:execute",)
    requires_confirmation = True
    idempotent = False
    parallel_safe = False
    timeout_seconds = 130
    retry_policy: ClassVar[dict] = {"max_attempts": 1, "backoff_seconds": 0}
    redact_fields = ("command",)
    version = "1"
    manages_own_timeout = True
    context_aware = True
    # A shell command may mutate any file or external state. Never cache it.
    cacheable = False

    def resource_keys(self, arguments: dict) -> tuple[str, ...]:
        return ("shell:global-cwd",)

    def execute(self, command: str, timeout: int = 120) -> str:
        """Legacy contract: return the model-readable command transcript."""
        return self._run_command(command, timeout, None).text

    def execute_with_context(
        self,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> str | ToolResult:
        """Stream output and honor the run's cancellation/deadline."""
        command = arguments["command"]
        timeout = arguments.get("timeout", 120)
        outcome = self._run_command(command, timeout, context)
        if outcome.cancelled:
            return ToolResult.failure(
                context.tool_call_id,
                self.name,
                outcome.text,
                outcome=ToolOutcome.CANCELLED,
                error_category="cancelled",
                error_code="COMMAND_CANCELLED",
                retryable=False,
            )
        if outcome.timed_out:
            return ToolResult.failure(
                context.tool_call_id,
                self.name,
                outcome.text,
                outcome=ToolOutcome.TIMEOUT,
                error_category="timeout",
                error_code="COMMAND_TIMEOUT",
                retryable=True,
            )
        return outcome.text

    def _run_command(
        self,
        command: str,
        timeout: int,
        context: ToolExecutionContext | None,
    ) -> _CommandOutcome:
        warning = _check_dangerous(command)
        if warning:
            text = (
                f"⚠ Blocked: {warning}\nCommand: {command}\n"
                "If intentional, modify the command to be more specific."
            )
            return _CommandOutcome(text, None)

        cwd = get_tracked_cwd() or os.getcwd()
        try:
            outcome = _run_streaming(command, timeout, cwd, context)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(
                "Error: command execution failed. Details were recorded in trace.",
                category="command_error",
                code="COMMAND_EXECUTION_FAILED",
                retryable=True,
                debug_message=str(e),
            ) from e

        if outcome.returncode == 0 and not outcome.timed_out and not outcome.cancelled:
            _update_cwd(command, cwd)
        return outcome


def _check_dangerous(cmd: str) -> str | None:
    """Return a warning string if the command looks destructive, else None."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd):
            return reason
    return None


def _split_words(statement: str) -> list[str]:
    """Split one statement into words on unquoted whitespace, stripping the
    quotes. Backslashes are kept verbatim (Windows paths must not lose them,
    so this deliberately does not use shlex's posix escape processing)."""
    words: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in statement:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def _split_statements(command: str) -> list[list[str]]:
    """Split a shell command into per-statement word lists on `&&` and `;`,
    honoring quotes. Words come back dequoted, so `cd "my dir"` arrives as
    ["cd", "my dir"] and a separator inside quotes never splits."""
    statements: list[str] = []
    quote: str | None = None
    start = 0
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        else:
            is_and = command[i : i + 2] == "&&"
            if ch == ";" or is_and:
                statement = command[start:i]
                if statement.strip():
                    statements.append(statement)
                i += 2 if is_and else 1
                start = i
                continue
        i += 1
    tail = command[start:]
    if tail.strip():
        statements.append(tail)
    return [_split_words(statement) for statement in statements]


def _update_cwd(command: str, current_cwd: str):
    """Track directory changes from cd commands, per thread."""
    # walk each cd in a && or ; chain, resolving relative targets against the dir the
    # previous cd landed in (not the original cwd) so `cd a && cd b` ends in a/b.
    # a parenthesized group is a subshell — `( cd a )` never changes this shell's
    # cwd, so scrub those before scanning; a bare `cd` goes home.
    scrubbed = re.sub(r"\([^()]*\)", " ", command)
    running = current_cwd
    changed = False
    for words in _split_statements(scrubbed):
        if words and words[0] == "cd":
            target = words[1] if len(words) > 1 else "~"
            new_dir = os.path.normpath(os.path.join(running, os.path.expanduser(target)))
            if os.path.isdir(new_dir):
                running = new_dir
                changed = True
    if changed:
        _local.cwd = running
