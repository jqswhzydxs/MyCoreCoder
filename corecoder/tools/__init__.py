"""Tool registry."""

from .agent import AgentTool
from .bash import BashTool
from .edit import EditFileTool
from .glob_tool import GlobTool
from .grep import GrepTool
from .protocol import (
    LegacyToolAdapter,
    ToolError,
    ToolExecutionContext,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from .read import ReadFileTool
from .todo import TodoWriteTool
from .write import WriteFileTool

ALL_TOOLS = [
    BashTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    TodoWriteTool(),
    AgentTool(),
]

__all__ = [
    "ALL_TOOLS",
    "LegacyToolAdapter",
    "ToolError",
    "ToolExecutionContext",
    "ToolOutcome",
    "ToolResult",
    "ToolSpec",
]

