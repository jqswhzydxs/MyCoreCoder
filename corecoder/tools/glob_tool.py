"""File pattern matching."""

import os
from pathlib import Path
from typing import ClassVar

from .base import Tool
from .protocol import ToolError


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files matching a glob pattern. "
        "Supports ** for recursive matching (e.g. '**/*.py')."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: cwd)",
            },
        },
        "required": ["pattern"],
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
        path = Path(arguments.get("path", ".")).expanduser().resolve()
        return (f"file:{os.path.normcase(str(path))}",)

    def execute(self, pattern: str, path: str = ".") -> str:
        try:
            base = Path(path).expanduser().resolve()
            if not base.exists():
                return f"Error: {path} not found"
            if not base.is_dir():
                return f"Error: {path} is not a directory"

            hits = list(base.glob(pattern))
            # sort by mtime, newest first
            hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

            total = len(hits)
            shown = hits[:100]
            lines = [str(h) for h in shown]
            result = "\n".join(lines)

            if total > 100:
                result += f"\n... ({total} matches, showing first 100)"
            return result or "No files matched."
        except Exception as e:
            raise ToolError(
                "Error: file search failed. Details were recorded in trace.",
                category="file_search_error",
                code="FILE_SEARCH_FAILED",
                debug_message=str(e),
            ) from e
