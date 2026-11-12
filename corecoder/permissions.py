"""User consent for tool calls, distilled from Claude Code's permissions.

The tools split in two. Read-only ones (read_file, glob, grep, todo_write)
run the moment the model asks; the mutating ones (edit_file, write_file,
bash, and spawning a sub-agent) stop for a yes first. "Always allow" is
remembered per tool, and the REPL passes a persist_path so the grant also
survives restarts via ~/.corecoder/permissions.json. Per tool rather than
per command, because one approved bash prefix says nothing about the next
command anyway.

When there is nobody to ask (one-shot -p mode, or a library embedding with
no callback), a mutating call is refused instead of blocking on input that
can never arrive. The refusal travels back as an ordinary tool result, so
the loop survives and the model can route around it.
"""

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .tools.protocol import ToolSpec

DEFAULT_STORE = Path.home() / ".corecoder" / "permissions.json"


@dataclass(slots=True)
class PermissionRequest:
    """One private worker-to-UI permission handoff."""

    request_id: str
    tool_name: str
    arguments: dict = field(repr=False)
    created_monotonic: float = field(default_factory=time.monotonic)
    _resolved: threading.Event = field(default_factory=threading.Event, repr=False)
    _verdict: str | None = field(default=None, repr=False)

    @property
    def argument_names(self) -> tuple[str, ...]:
        return tuple(sorted(str(name) for name in self.arguments))


class PermissionRequestBroker:
    """FIFO permission arbiter with no terminal input of its own.

    Agent workers block on per-request events.  The CLI main thread is the only
    party that reads y/a/n and resolves those requests.
    """

    _VERDICTS = frozenset({"once", "always", "deny"})

    def __init__(self, *, timeout: float | None = None) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("permission timeout must be non-negative")
        self.timeout = timeout
        self._condition = threading.Condition()
        self._pending: deque[PermissionRequest] = deque()
        self._requests: dict[str, PermissionRequest] = {}
        self._closed = False
        self._wakeup = None

    def set_wakeup(self, callback) -> None:
        """Register a best-effort UI invalidation callback."""
        with self._condition:
            self._wakeup = callback

    def request(self, tool_name: str, arguments: dict) -> str:
        request = PermissionRequest(
            request_id=uuid.uuid4().hex,
            tool_name=str(tool_name),
            arguments=dict(arguments),
        )
        with self._condition:
            if self._closed:
                return "deny"
            self._pending.append(request)
            self._requests[request.request_id] = request
            self._condition.notify_all()
            wakeup = self._wakeup
        self._notify(wakeup)
        if not request._resolved.wait(self.timeout):
            self.resolve(request.request_id, "deny")
        return request._verdict or "deny"

    def poll(self, timeout: float | None = None) -> PermissionRequest | None:
        """Return the oldest unresolved request, optionally waiting for one."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._pending and not self._closed:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._pending[0] if self._pending else None

    def peek(self) -> PermissionRequest | None:
        return self.poll(timeout=0)

    def resolve(self, request_id: str, verdict: str) -> bool:
        selected = str(verdict).lower()
        if selected not in self._VERDICTS:
            raise ValueError(f"unsupported permission verdict: {verdict}")
        with self._condition:
            request = self._requests.pop(str(request_id), None)
            if request is None or request._resolved.is_set():
                return False
            request._verdict = selected
            request._resolved.set()
            self._pending = deque(
                item for item in self._pending if item.request_id != request.request_id
            )
            self._condition.notify_all()
            wakeup = self._wakeup
        self._notify(wakeup)
        return True

    def deny_all(self) -> int:
        with self._condition:
            request_ids = [request.request_id for request in self._pending]
        return sum(self.resolve(request_id, "deny") for request_id in request_ids)

    def close(self) -> None:
        self.deny_all()
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            wakeup = self._wakeup
        self._notify(wakeup)

    @property
    def has_pending(self) -> bool:
        with self._condition:
            return bool(self._pending)

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    @staticmethod
    def _notify(callback) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - UI wakeups must fail open
            return


class Permission:
    """Session-scoped consent state. No prompts itself: the CLI hands in
    `ask`, and the only I/O is the optional always-allow store."""

    READ_ONLY = frozenset({"read_file", "glob", "grep", "todo_write"})

    def __init__(
        self,
        ask=None,
        allow_all: bool = False,
        persist_path: Path | None = None,
        broker: PermissionRequestBroker | None = None,
    ):
        # ask(tool_name, arguments) -> "once" | "always" | "deny"
        self.ask = ask
        self.broker = broker
        self.allow_all = allow_all
        self._always: set[str] = set()
        self._persist_path = persist_path
        if persist_path is not None:
            try:
                saved = json.loads(persist_path.read_text(encoding="utf-8"))
                self._always = {t for t in saved.get("always", []) if isinstance(t, str)}
            except (OSError, ValueError, AttributeError):
                pass  # a missing or corrupt store just means nothing granted yet

    def check(
        self,
        tool_name: str,
        arguments: dict,
        tool_spec: ToolSpec | None = None,
    ) -> str | None:
        """Decide one call. None lets it through; a string is the refusal
        the model receives as its tool result."""
        spec_allows_without_prompt = (
            tool_spec is not None and not tool_spec.requires_confirmation
        )
        legacy_read_only = tool_spec is None and tool_name in self.READ_ONLY
        if (
            spec_allows_without_prompt
            or legacy_read_only
            or self.allow_all
            or tool_name in self._always
        ):
            return None
        if self.ask is None and self.broker is None:
            return (
                f"Permission denied: {tool_name} requires confirmation and this session "
                "is non-interactive, so nobody can approve it. Rerun with --yes, or "
                "tell the user the exact step so they can run it themselves."
            )
        verdict = (
            self.broker.request(tool_name, arguments)
            if self.broker is not None
            else self.ask(tool_name, arguments)
        )
        if verdict == "always":
            self._always.add(tool_name)
            self._save()
            return None
        if verdict == "once":
            return None
        return (
            f"Permission denied: the user refused this {tool_name} call. "
            "Do not retry it unchanged; ask what they would prefer instead."
        )

    def _save(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(
                json.dumps({"always": sorted(self._always)}, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # the grant still stands for the rest of this session
