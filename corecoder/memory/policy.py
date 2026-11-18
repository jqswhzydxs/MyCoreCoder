"""Deterministic write gates. Proposals cannot attest sources or grant permissions."""

from __future__ import annotations

import re

from .models import MemoryError, SourceAuthority, bounded, memory_key

_SECRETS = re.compile(
    r"(?i)(?:\b(?:sk|rk|pk)-[a-z0-9_-]{8,}|\bAKIA[A-Z0-9]{16}\b|"
    r"\bbearer\s+\S{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[_ -]?key|access[_ -]?token|password|passwd|secret|authorization)\s*[:=]\s*\S+)"
)
_AUTHORITY_KEYS = frozenset({"permission", "permissions", "allow_all", "tools", "tool_whitelist", "commands", "api_key"})


def check_content(content: str, *, maximum: int = 2000) -> str:
    content = bounded(content, maximum, "content")
    if _SECRETS.search(content):
        raise MemoryError("sensitive_content", "memory content contains a possible credential")
    return content


def check_candidate(kind: str, key: str, content: str, source: SourceAuthority) -> tuple[str, str]:
    key, content = memory_key(key), check_content(content)
    check_content(key)
    if key.split(".")[0] in _AUTHORITY_KEYS:
        raise MemoryError("policy_rejected", "memory cannot configure tools or permissions")
    if kind not in {"preference", "experience"}:
        raise MemoryError("policy_rejected", "unknown memory kind")
    if kind == "experience" and source.kind != "run":
        raise MemoryError("unverified_source", "experience requires an independently verified run")
    if kind == "preference" and source.kind not in {"user", "file"}:
        raise MemoryError("policy_rejected", "preferences require an explicit user request or authorized project rule")
    return key, content
