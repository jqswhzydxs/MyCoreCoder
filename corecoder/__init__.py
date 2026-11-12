"""CoreCoder - Minimal AI coding agent inspired by Claude Code's architecture."""

__version__ = "0.8.0"

from corecoder.agent import Agent
from corecoder.budget import (
    BudgetUsage,
    CancellationToken,
    RunBudget,
    action_fingerprint,
    canonicalize_arguments,
)
from corecoder.config import Config
from corecoder.events import (
    AgentEvent,
    AgentEventType,
    CallbackEventSink,
    CompositeEventSink,
    EventEmitter,
    NullEventSink,
    QueueEventSink,
)
from corecoder.llm import LLM
from corecoder.metrics import RunMetrics
from corecoder.permissions import PermissionRequest, PermissionRequestBroker
from corecoder.run_store import (
    DEFAULT_RUN_STORE,
    RunNotFoundError,
    RunStore,
    RunStoreCorruptError,
    RunStoreError,
    RunStoreVersionError,
    ToolCallStatus,
    make_idempotency_key,
)
from corecoder.runtime import (
    AgentBusyError,
    AgentRunHandle,
    ControlInbox,
    RunControlRejectedError,
    SteeringInstruction,
    SteeringMode,
    SteeringRejectedError,
)
from corecoder.state import (
    AgentResult,
    ExitReason,
    InvalidStateTransition,
    RunState,
    RunStatus,
    VerificationEvidenceRequired,
)
from corecoder.tool_cache import ToolCache
from corecoder.tools import (
    ALL_TOOLS,
    LegacyToolAdapter,
    ToolError,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from corecoder.verification import (
    CommandExitCodeVerifier,
    CompositeVerifier,
    FileStateVerifier,
    NoFailedToolCallVerifier,
    RequiredEvidenceVerifier,
    TestResultVerifier,
    VerificationEvidence,
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
    Verifier,
)

__all__ = [
    "ALL_TOOLS",
    "DEFAULT_RUN_STORE",
    "LLM",
    "Agent",
    "AgentBusyError",
    "AgentEvent",
    "AgentEventType",
    "AgentResult",
    "AgentRunHandle",
    "BudgetUsage",
    "CallbackEventSink",
    "CancellationToken",
    "CommandExitCodeVerifier",
    "CompositeEventSink",
    "CompositeVerifier",
    "Config",
    "ControlInbox",
    "EventEmitter",
    "ExitReason",
    "FileStateVerifier",
    "InvalidStateTransition",
    "LegacyToolAdapter",
    "NoFailedToolCallVerifier",
    "NullEventSink",
    "PermissionRequest",
    "PermissionRequestBroker",
    "QueueEventSink",
    "RequiredEvidenceVerifier",
    "RunBudget",
    "RunControlRejectedError",
    "RunMetrics",
    "RunNotFoundError",
    "RunState",
    "RunStatus",
    "RunStore",
    "RunStoreCorruptError",
    "RunStoreError",
    "RunStoreVersionError",
    "SteeringInstruction",
    "SteeringMode",
    "SteeringRejectedError",
    "TestResultVerifier",
    "ToolCache",
    "ToolCallStatus",
    "ToolError",
    "ToolOutcome",
    "ToolResult",
    "ToolSpec",
    "VerificationEvidence",
    "VerificationEvidenceRequired",
    "VerificationRequest",
    "VerificationResult",
    "VerificationStatus",
    "Verifier",
    "__version__",
    "action_fingerprint",
    "canonicalize_arguments",
    "make_idempotency_key",
]
