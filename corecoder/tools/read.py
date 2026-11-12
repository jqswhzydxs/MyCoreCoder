"""File reading with line numbers."""

import os
from pathlib import Path
from typing import ClassVar

from .base import Tool
from .protocol import ToolError


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a file's contents with line numbers. "
        "Always read a file before editing it."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file",
            },
            "offset": {
                "type": "integer",
                "description": "Start line (1-based). Default 1.",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read. Default 2000.",
            },
        },
        "required": ["file_path"],
    }
    side_effect = "read"
    risk = "low"
    required_permissions = ("filesystem:read",)
    requires_confirmation = False
    idempotent = True
    parallel_safe = True
    timeout_seconds = 30
    retry_policy: ClassVar[dict] = {"max_attempts": 1, "backoff_seconds": 0}
    redact_fields = ()
    version = "1"

    def resource_keys(self, arguments: dict) -> tuple[str, ...]:
        path = Path(arguments.get("file_path", ".")).expanduser().resolve()
        return (f"file:{os.path.normcase(str(path))}",)

    def execute(self, file_path: str, offset: int = 1, limit: int = 2000) -> str:
        try:
            p = Path(file_path).expanduser().resolve()
            if not p.exists():
                return f"Error: {file_path} not found"
            if not p.is_file():
                return f"Error: {file_path} is a directory, not a file"

            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            total = len(lines)

            start = max(0, offset - 1)
            chunk = lines[start : start + limit]
            numbered = [f"{start + i + 1}\t{ln}" for i, ln in enumerate(chunk)]
            result = "\n".join(numbered)

            if total > start + limit:
                result += f"\n... ({total} lines total, showing {start+1}-{start+len(chunk)})"
            return result or "(empty file)"
        except Exception as e:
            raise ToolError(
                "Error: file could not be read. Details were recorded in trace.",
                category="file_read_error",
                code="FILE_READ_FAILED",
                debug_message=str(e),
            ) from e
