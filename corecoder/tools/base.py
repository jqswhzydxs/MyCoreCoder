"""Base class for legacy and structured tools."""

import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, ClassVar

from .protocol import ToolExecutionContext, ToolSpec

# File-mutating tools (write_file/edit_file) serialize on this, so a parallel
# batch can't interleave read-modify-write on the same file and lose an edit.
FILE_MUTATION_LOCK = threading.Lock()


class Tool(ABC):
    """Minimal interface; old string-returning subclasses remain valid."""

    name: str
    description: str
    parameters: dict  # JSON Schema for the function args
    output_schema: ClassVar[dict[str, Any]] = {"type": "string"}
    side_effect = "external"
    risk = "high"
    required_permissions: ClassVar[tuple[str, ...]] = ("tool:execute",)
    requires_confirmation = True
    idempotent = False
    parallel_safe = False
    timeout_seconds: float | None = None
    retry_policy: ClassVar[dict[str, Any]] = {"max_attempts": 1, "backoff_seconds": 0}
    redact_fields: ClassVar[tuple[str, ...]] = ()
    version = "1"
    manages_own_timeout = False
    # Opt-in: legacy, MCP and third-party tools keep their original contract.
    context_aware = False
    # Read-only tools are cached by default; mutating/external specs are
    # filtered by ToolCache regardless of this flag.
    cacheable = True

    @abstractmethod
    def execute(self, **kwargs):
        """Run the tool and return str or ToolResult."""
        ...

    def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ):
        """Context-aware tools override this; the default preserves legacy."""
        return self.execute(**arguments)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.parameters,
            output_schema=self.output_schema,
            side_effect=self.side_effect,
            risk=self.risk,
            required_permissions=self.required_permissions,
            requires_confirmation=self.requires_confirmation,
            idempotent=self.idempotent,
            parallel_safe=self.parallel_safe,
            timeout_seconds=self.timeout_seconds,
            retry_policy=self.retry_policy,
            redact_fields=self.redact_fields,
            version=self.version,
            cacheable=self.cacheable,
        )

    def schema(self) -> dict:
        """OpenAI schema only; governance metadata stays runtime-private."""
        return self.spec.function_schema()

    def resource_keys(self, arguments: dict[str, Any]) -> tuple[str, ...]:
        """Resources touched by one call; subclasses may provide finer keys."""
        return ()

    def data_dependencies(self, arguments: dict[str, Any]) -> Iterable[str]:
        """Prior tool-call ids required by this invocation, if any."""
        return ()
