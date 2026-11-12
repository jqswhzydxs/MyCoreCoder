"""File creation / overwrite."""

import os
from pathlib import Path
from typing import ClassVar

from ..checkpoints import record as _record_checkpoint
from .base import Tool
from .edit import _changed_files
from .protocol import ToolError


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one. "
        "For small edits to existing files, prefer edit_file instead."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path for the file",
            },
            "content": {
                "type": "string",
                "description": "Full file content to write",
            },
        },
        "required": ["file_path", "content"],
    }
    side_effect = "write"
    risk = "high"
    required_permissions = ("filesystem:write",)
    requires_confirmation = True
    idempotent = True
    parallel_safe = False
    timeout_seconds = 30
    retry_policy: ClassVar[dict] = {"max_attempts": 1, "backoff_seconds": 0}
    redact_fields = ("content",)
    version = "1"

    def resource_keys(self, arguments: dict) -> tuple[str, ...]:
        path = Path(arguments.get("file_path", ".")).expanduser().resolve()
        return (f"file:{os.path.normcase(str(path))}",)

    def execute(self, file_path: str, content: str) -> str:
        from .base import FILE_MUTATION_LOCK

        try:
            with FILE_MUTATION_LOCK:
                p = Path(file_path).expanduser().resolve()
                p.parent.mkdir(parents=True, exist_ok=True)
                _record_checkpoint(p)
                p.write_text(content, encoding="utf-8")
                _changed_files.add(str(p))
            n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            return f"Wrote {n_lines} lines to {file_path}"
        except Exception as e:
            raise ToolError(
                "Error: file could not be written. Details were recorded in trace.",
                category="file_write_error",
                code="FILE_WRITE_FAILED",
                debug_message=str(e),
            ) from e
