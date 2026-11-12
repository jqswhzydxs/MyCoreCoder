"""Safe, revision-scoped cache for read-only tool results.

The cache is intentionally conservative:

* only successful, idempotent, non-mutating tools are eligible;
* the key includes the tool name/version, canonical arguments, cwd and the
  controller's workspace revision;
* any write or external call invalidates every entry and bumps the revision;
* Bash and tools that explicitly disable caching never reach this module.

Cached values are cloned and rebound to the current ``tool_call_id`` so model
content, evidence and budget/trace accounting keep their normal shape.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from corecoder.budget import canonicalize_arguments
from corecoder.tools.protocol import ToolResult, ToolSpec

_CACHEABLE_SIDE_EFFECTS = frozenset({"none", "read"})


class ToolCache:
    """Thread-safe LRU cache with per-key single-flight execution."""

    def __init__(self, *, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self.enabled = True
        self._entries: OrderedDict[str, ToolResult] = OrderedDict()
        self._revision = 0
        self._lock = threading.RLock()
        self._key_locks: dict[str, threading.Lock] = {}
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def is_cacheable(self, spec: ToolSpec) -> bool:
        """Return whether this tool declaration permits caching at all."""
        return bool(
            self.enabled
            and spec.cacheable
            and spec.idempotent
            and spec.side_effect in _CACHEABLE_SIDE_EFFECTS
        )

    def make_key(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        *,
        cwd: str | None = None,
    ) -> str:
        """Build a stable key including tool version, cwd and revision."""
        payload = {
            "tool_name": spec.name,
            "tool_version": spec.version,
            "arguments": json.loads(canonicalize_arguments(arguments)),
            "cwd": os.path.abspath(cwd or os.getcwd()),
            "workspace_revision": self.revision,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str, *, call_id: str) -> ToolResult | None:
        """Return a call-id-rebound copy of a cached result, if present."""
        with self._lock:
            result = self._entries.get(key)
            if result is None:
                return None
            self._entries.move_to_end(key)
            return _rebind(result, call_id)

    def put(self, key: str, result: ToolResult) -> None:
        """Store a successful result, evicting the least recently used entry."""
        if not result.ok:
            return
        with self._lock:
            self._entries[key] = _freeze(result)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, reason: str = "workspace_changed") -> int:
        """Drop every entry and advance the workspace revision."""
        del reason  # Kept in the signature for readable call sites.
        with self._lock:
            self._revision += 1
            self._entries.clear()
            self.invalidations += 1
            return self._revision

    def clear(self) -> None:
        """Drop entries without advancing the revision (diagnostics/tests)."""
        with self._lock:
            self._entries.clear()

    def get_or_execute(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        *,
        call_id: str,
        cwd: str | None = None,
        execute: Callable[[], ToolResult],
    ) -> tuple[bool, ToolResult, str]:
        """Return ``(cache_hit, result, key)``, executing at most once per key.

        Identical concurrent calls share one execution through a per-call-key
        lock.  A caller that arrives while the first is running waits, then
        reads the newly stored value instead of duplicating the work.
        """
        if not self.is_cacheable(spec):
            return False, execute(), ""

        cwd_value = os.path.abspath(cwd or os.getcwd())
        lock_key = _call_identity(spec, arguments, cwd_value)
        with self._lock:
            key_lock = self._key_locks.setdefault(lock_key, threading.Lock())

        with key_lock:
            # Recompute after waiting: a concurrent mutation may have bumped
            # the revision while this caller was blocked.
            key = self.make_key(spec, arguments, cwd=cwd_value)
            cached = self.get(key, call_id=call_id)
            if cached is not None:
                with self._lock:
                    self.hits += 1
                return True, cached, key
            with self._lock:
                self.misses += 1
            result = execute()
            self.put(key, result)
            return False, result, key

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "invalidations": self.invalidations,
                "revision": self._revision,
            }


def _call_identity(
    spec: ToolSpec,
    arguments: dict[str, Any],
    cwd: str,
) -> str:
    payload = {
        "tool_name": spec.name,
        "tool_version": spec.version,
        "arguments": json.loads(canonicalize_arguments(arguments)),
        "cwd": cwd,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _freeze(result: ToolResult) -> ToolResult:
    return replace(result, data=copy.deepcopy(result.data))


def _rebind(result: ToolResult, call_id: str) -> ToolResult:
    evidence = tuple(
        f"tool:{call_id}:{result.tool_name}" if item.startswith("tool:") else item
        for item in result.evidence
    )
    return replace(
        result,
        call_id=call_id,
        data=copy.deepcopy(result.data),
        evidence=evidence,
    )
