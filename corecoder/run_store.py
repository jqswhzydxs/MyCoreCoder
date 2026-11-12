"""Transactional SQLite persistence and conservative recovery for Agent runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .budget import canonicalize_arguments, result_summary
from .tools.protocol import ToolResult
from .trace import _is_sensitive_key

SCHEMA_VERSION = 1
DEFAULT_RUN_STORE = Path.home() / ".corecoder" / "runs.db"
_REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|pk)-[a-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]{8,}"),
    re.compile(
        r"(?i)((?:access[_ -]?token|api[_ -]?key|authorization|password|secret)"
        r"\s*[:=]\s*)[^\s,;]+"
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class ToolCallStatus(str, Enum):
    PLANNED = "PLANNED"
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class RunStoreError(RuntimeError):
    """Base class for safe, user-facing persistence failures."""


class RunStoreCorruptError(RunStoreError):
    pass


class RunStoreVersionError(RunStoreError):
    pass


class RunNotFoundError(RunStoreError):
    pass


class InjectedProcessCrash(BaseException):
    """Test-only process-crash stand-in that Agent deliberately does not catch."""


@dataclass(frozen=True, slots=True)
class StoredRun:
    run_id: str
    state: dict[str, Any]
    messages: list[dict[str, Any]]
    model: str | None
    trace_ref: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class StoredSteeringControl:
    """Durable lifecycle record for one steering instruction.

    The instruction body is deliberately absent: control metadata is enough to
    detect a received-but-unapplied instruction after a crash, without turning
    the run database into a transcript of potentially sensitive user text.
    """

    run_id: str
    instruction_id: str
    mode: str
    status: str
    received_sequence: int
    applied_sequence: int | None
    received_at: str
    applied_at: str | None
    safe_point: str | None
    skipped_tool_count: int
    needs_action_reason: str | None
    run_status: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StoredToolCall:
    run_id: str
    tool_call_id: str
    logical_step: str
    tool_name: str
    tool_version: str
    side_effect: str
    idempotent: bool
    status: ToolCallStatus
    idempotency_key: str
    arguments: dict[str, Any]
    replay_arguments: dict[str, Any] | None
    result: dict[str, Any] | None
    retryable: bool
    attempt: int
    max_attempts: int
    target_path: str | None
    pre_hash: str | None
    expected_hash: str | None
    checkpoint_ref: str | None
    recovery_note: str | None


def _redact_string(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda match: (
                f"{match.group(1)}{_REDACTED}" if match.lastindex else _REDACTED
            ),
            result,
        )
    return result


def _redact(value: Any, *, explicit_fields: frozenset[str] = frozenset()) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                _REDACTED
                if str(key) in explicit_fields or _is_sensitive_key(key)
                else _redact(item, explicit_fields=explicit_fields)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item, explicit_fields=explicit_fields) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist valid but argument-free tool history, never full tool parameters."""
    safe = _redact(messages)
    for message in safe:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and "arguments" in function:
                function["arguments"] = '{"_persisted":"[REDACTED]"}'
    return safe


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RunStoreCorruptError(
            "Run store contains invalid persisted JSON; preserve it for inspection "
            "and recover from a known-good copy."
        ) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_hash(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes()) if path.is_file() else None
    except OSError:
        return None


def make_idempotency_key(
    run_id: str,
    logical_step: str,
    tool_version: str,
    arguments: Mapping[str, Any],
) -> str:
    """Hash the stable identity required for one logical side-effect attempt."""
    payload = "\n".join(
        (run_id, logical_step, tool_version, canonicalize_arguments(arguments))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RunStore:
    """A small SQLite journal. Every write is one explicit transaction."""

    def __init__(self, path: str | Path = DEFAULT_RUN_STORE):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            with connection:
                yield connection
        except RunStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise RunStoreCorruptError(
                f"Run store {self.path} is corrupt or unreadable; preserve it for "
                "inspection and start with a new database."
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def _initialise(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                existing = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS metadata "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if row is None:
                    unexpected = existing - {"metadata"}
                    if unexpected:
                        raise RunStoreVersionError(
                            "Run store has tables but no schema_version; refusing unsafe recovery."
                        )
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif row[0] != str(SCHEMA_VERSION):
                    raise RunStoreVersionError(
                        f"Run store schema {row[0]} is incompatible with supported "
                        f"schema {SCHEMA_VERSION}; migrate or select another database."
                    )
                self._create_schema(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except RunStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise RunStoreCorruptError(
                f"Run store {self.path} is corrupt or unreadable; preserve it for "
                "inspection and start with a new database."
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                goal TEXT NOT NULL,
                success_criteria_json TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step TEXT NOT NULL,
                round INTEGER NOT NULL,
                state_version INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                model TEXT,
                trace_ref TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                exit_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                state_version INTEGER NOT NULL,
                boundary TEXT NOT NULL,
                state_json TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS state_versions_run_idx
                ON state_versions(run_id, id);
            CREATE TABLE IF NOT EXISTS tool_calls (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                tool_call_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                logical_step TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                side_effect TEXT NOT NULL,
                idempotent INTEGER NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                arguments_sha256 TEXT NOT NULL,
                replay_arguments_json TEXT,
                result_json TEXT,
                retryable INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                target_path TEXT,
                pre_hash TEXT,
                expected_hash TEXT,
                checkpoint_ref TEXT,
                recovery_note TEXT,
                planned_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, tool_call_id),
                UNIQUE(run_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS tool_calls_status_idx
                ON tool_calls(run_id, status);
            CREATE TABLE IF NOT EXISTS evidence (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                evidence_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id, evidence_id)
            );
            CREATE TABLE IF NOT EXISTS event_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                trace_id TEXT,
                event_type TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                external_ref TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS steering_controls (
                run_id TEXT NOT NULL,
                instruction_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                received_sequence INTEGER NOT NULL,
                applied_sequence INTEGER,
                received_at TEXT NOT NULL,
                applied_at TEXT,
                safe_point TEXT,
                skipped_tool_count INTEGER NOT NULL DEFAULT 0,
                needs_action_reason TEXT,
                run_status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, instruction_id)
            );
            CREATE INDEX IF NOT EXISTS steering_controls_status_idx
                ON steering_controls(run_id, status, received_sequence);
            """
        )

    def save_boundary(
        self,
        state,
        messages: list[dict[str, Any]],
        boundary: str,
        *,
        model: str | None = None,
        trace_id: str | None = None,
        trace_ref: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            self._save_boundary(
                connection,
                state,
                messages,
                boundary,
                model=model,
                trace_id=trace_id,
                trace_ref=trace_ref,
            )

    def record_steering_received(
        self,
        run_id: str,
        instruction_id: str,
        *,
        mode: str,
        received_sequence: int,
        received_at: str | None = None,
        run_status: str = "running",
    ) -> None:
        """Journal receipt before the Agent can apply the instruction.

        The body is intentionally not accepted by this API.  Keeping the
        control log free of instruction text makes a leaked database much less
        damaging while still supporting crash detection.
        """
        if mode not in {"append", "redirect"}:
            raise ValueError(f"unsupported steering mode: {mode!r}")
        if received_sequence <= 0:
            raise ValueError("received_sequence must be positive")
        now = received_at or _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO steering_controls(
                    run_id, instruction_id, schema_version, mode, status,
                    received_sequence, applied_sequence, received_at, applied_at,
                    safe_point, skipped_tool_count, needs_action_reason,
                    run_status, updated_at
                ) VALUES (?, ?, ?, ?, 'received', ?, NULL, ?, NULL, NULL, 0, NULL, ?, ?)
                ON CONFLICT(run_id, instruction_id) DO UPDATE SET
                    mode = excluded.mode,
                    received_sequence = MIN(
                        steering_controls.received_sequence,
                        excluded.received_sequence
                    ),
                    run_status = excluded.run_status,
                    updated_at = excluded.updated_at
                WHERE steering_controls.status = 'received'
                """,
                (
                    str(run_id),
                    str(instruction_id),
                    SCHEMA_VERSION,
                    mode,
                    int(received_sequence),
                    now,
                    str(run_status),
                    now,
                ),
            )

    def mark_steering_applied(
        self,
        run_id: str,
        instruction_id: str,
        *,
        applied_sequence: int,
        safe_point: str,
        applied_at: str | None = None,
        skipped_tool_count: int = 0,
        run_status: str = "running",
    ) -> None:
        """Mark a previously received instruction as applied at a safe point."""
        if applied_sequence <= 0:
            raise ValueError("applied_sequence must be positive")
        now = applied_at or _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE steering_controls
                SET status = 'applied',
                    applied_sequence = ?,
                    applied_at = ?,
                    safe_point = ?,
                    skipped_tool_count = ?,
                    run_status = ?,
                    needs_action_reason = NULL,
                    updated_at = ?
                WHERE run_id = ? AND instruction_id = ? AND status = 'received'
                """,
                (
                    int(applied_sequence),
                    now,
                    str(safe_point),
                    max(0, int(skipped_tool_count)),
                    str(run_status),
                    now,
                    str(run_id),
                    str(instruction_id),
                ),
            )

    def mark_steering_needs_action(
        self,
        run_id: str,
        instruction_ids: list[str],
        *,
        reason: str,
        run_status: str = "needs_action",
    ) -> None:
        """Require explicit user action instead of replaying an unknown control."""
        now = _utc_now()
        with self._transaction() as connection:
            connection.executemany(
                """
                UPDATE steering_controls
                SET status = 'needs_action',
                    needs_action_reason = ?,
                    run_status = ?,
                    updated_at = ?
                WHERE run_id = ? AND instruction_id = ? AND status = 'received'
                """,
                [
                    (str(reason), str(run_status), now, str(run_id), str(instruction_id))
                    for instruction_id in instruction_ids
                ],
            )

    def finalize_steering(self, run_id: str, run_status: str) -> None:
        """Stamp every control row with the run's final status.

        A received instruction that never reached a safe point must not look
        like it merely disappeared.  Terminal runs mark it as requiring an
        explicit operator decision; an applied instruction keeps its receipt.
        """
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE steering_controls
                SET status = CASE
                        WHEN status = 'received' THEN 'needs_action'
                        ELSE status
                    END,
                    needs_action_reason = CASE
                        WHEN status = 'received' THEN COALESCE(
                            needs_action_reason,
                            'run terminated before the instruction reached a safe point'
                        )
                        ELSE needs_action_reason
                    END,
                    run_status = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (str(run_status), now, str(run_id)),
            )

    def pending_steering(self, run_id: str) -> list[StoredSteeringControl]:
        """Return received instructions that never reached a safe point."""
        return self._steering_controls(run_id, status="received")

    def steering_controls(self, run_id: str) -> list[StoredSteeringControl]:
        return self._steering_controls(run_id, status=None)

    def _steering_controls(
        self, run_id: str, *, status: str | None
    ) -> list[StoredSteeringControl]:
        query = """
            SELECT run_id, instruction_id, mode, status, received_sequence,
                   applied_sequence, received_at, applied_at, safe_point,
                   skipped_tool_count, needs_action_reason, run_status, updated_at
            FROM steering_controls
            WHERE run_id = ?
        """
        parameters: list[Any] = [str(run_id)]
        if status is not None:
            query += " AND status = ?"
            parameters.append(status)
        query += " ORDER BY received_sequence, instruction_id"
        with self._transaction() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            StoredSteeringControl(
                run_id=row["run_id"],
                instruction_id=row["instruction_id"],
                mode=row["mode"],
                status=row["status"],
                received_sequence=int(row["received_sequence"]),
                applied_sequence=(
                    int(row["applied_sequence"])
                    if row["applied_sequence"] is not None
                    else None
                ),
                received_at=row["received_at"],
                applied_at=row["applied_at"],
                safe_point=row["safe_point"],
                skipped_tool_count=int(row["skipped_tool_count"]),
                needs_action_reason=row["needs_action_reason"],
                run_status=row["run_status"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def record_steering(
        self,
        state,
        messages: list[dict[str, Any]],
        *,
        instruction_ids: list[str],
        modes: list[str],
        safe_point: str,
        model: str | None = None,
        trace_id: str | None = None,
        trace_ref: str | None = None,
    ) -> None:
        """Compatibility persistence boundary for applied steering.

        The state snapshot carries redacted steering metadata.  A dedicated
        control-log table can be added later without changing Agent's call
        site or storing raw instruction text in this boundary API.
        """
        dominant_mode = "redirect" if "redirect" in modes else "append"
        boundary = (
            f"steering_applied:{dominant_mode}:{safe_point}:"
            f"{len(instruction_ids)}"
        )
        self.save_boundary(
            state,
            messages,
            boundary,
            model=model,
            trace_id=trace_id,
            trace_ref=trace_ref,
        )

    def _save_boundary(
        self,
        connection: sqlite3.Connection,
        state,
        messages: list[dict[str, Any]],
        boundary: str,
        *,
        model: str | None,
        trace_id: str | None,
        trace_ref: str | None,
    ) -> None:
        now = _utc_now()
        state_payload = _redact(state.to_dict())
        safe_messages = _sanitize_messages(messages)
        state_json = _json(state_payload)
        messages_json = _json(safe_messages)
        criteria_json = _json(_redact(state.success_criteria))
        connection.execute(
            """
            INSERT INTO runs(
                run_id, schema_version, goal, success_criteria_json, status,
                current_step, round, state_version, state_json, messages_json,
                model, trace_ref, started_at, finished_at, exit_reason,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                schema_version=excluded.schema_version,
                goal=excluded.goal,
                success_criteria_json=excluded.success_criteria_json,
                status=excluded.status,
                current_step=excluded.current_step,
                round=excluded.round,
                state_version=excluded.state_version,
                state_json=excluded.state_json,
                messages_json=excluded.messages_json,
                model=COALESCE(excluded.model, runs.model),
                trace_ref=COALESCE(excluded.trace_ref, runs.trace_ref),
                finished_at=excluded.finished_at,
                exit_reason=excluded.exit_reason,
                updated_at=excluded.updated_at
            """,
            (
                state.run_id,
                SCHEMA_VERSION,
                _redact_string(state.goal),
                criteria_json,
                state.status.value,
                state.current_step,
                state.round,
                state.state_version,
                state_json,
                messages_json,
                model,
                trace_ref,
                state.started_at,
                state.finished_at,
                state.exit_reason.value if state.exit_reason else None,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO state_versions(
                schema_version, run_id, state_version, boundary,
                state_json, messages_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                SCHEMA_VERSION,
                state.run_id,
                state.state_version,
                boundary,
                state_json,
                messages_json,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO event_refs(
                schema_version, run_id, trace_id, event_type,
                state_version, external_ref, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                SCHEMA_VERSION,
                state.run_id,
                trace_id,
                boundary,
                state.state_version,
                trace_ref,
                now,
            ),
        )
        for item in state.verification_evidence:
            evidence_id = str(item.get("evidence_id") or hashlib.sha256(
                _json(_redact(item)).encode("utf-8")
            ).hexdigest())
            connection.execute(
                """
                INSERT OR REPLACE INTO evidence(
                    run_id, evidence_id, schema_version, evidence_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    state.run_id,
                    evidence_id,
                    SCHEMA_VERSION,
                    _json(_redact(item)),
                    now,
                ),
            )

    def plan_tool_calls(
        self,
        state,
        messages: list[dict[str, Any]],
        tool_calls,
        tools: Mapping[str, Any],
        *,
        model: str | None = None,
        trace_id: str | None = None,
        trace_ref: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            self._save_boundary(
                connection,
                state,
                messages,
                "after_model_before_tools",
                model=model,
                trace_id=trace_id,
                trace_ref=trace_ref,
            )
            now = _utc_now()
            for index, call in enumerate(tool_calls):
                tool = tools.get(call.name)
                spec = tool.spec if tool is not None else None
                version = spec.version if spec is not None else "unknown"
                side_effect = spec.side_effect if spec is not None else "external"
                idempotent = bool(spec.idempotent) if spec is not None else False
                redact_fields = frozenset(spec.redact_fields if spec is not None else ())
                original = json.loads(canonicalize_arguments(call.arguments))
                opaque_external = spec is None or (
                    spec.side_effect == "external" and spec.risk == "high"
                )
                safe_arguments = (
                    {str(key): _REDACTED for key in original}
                    if opaque_external
                    else _redact(original, explicit_fields=redact_fields)
                )
                replay_arguments = original if safe_arguments == original else None
                logical_step = f"round:{state.round}:call:{index}:{call.name}"
                key = make_idempotency_key(state.run_id, logical_step, version, original)
                target_path, pre_hash, expected_hash, checkpoint_ref = (
                    self._file_metadata(call.name, original)
                )
                max_attempts = 1
                if spec is not None:
                    max_attempts = max(1, int(spec.retry_policy.get("max_attempts", 1)))
                connection.execute(
                    """
                    INSERT INTO tool_calls(
                        run_id, tool_call_id, schema_version, logical_step,
                        tool_name, tool_version, side_effect, idempotent, status,
                        idempotency_key, arguments_json, arguments_sha256,
                        replay_arguments_json, retryable, attempt, max_attempts,
                        target_path, pre_hash, expected_hash, checkpoint_ref,
                        planned_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, tool_call_id) DO NOTHING
                    """,
                    (
                        state.run_id,
                        call.id,
                        SCHEMA_VERSION,
                        logical_step,
                        call.name,
                        version,
                        side_effect,
                        int(idempotent),
                        ToolCallStatus.PLANNED.value,
                        key,
                        _json(safe_arguments),
                        hashlib.sha256(canonicalize_arguments(original).encode("utf-8")).hexdigest(),
                        _json(replay_arguments) if replay_arguments is not None else None,
                        max_attempts,
                        target_path,
                        pre_hash,
                        expected_hash,
                        checkpoint_ref,
                        now,
                        now,
                    ),
                )

    @staticmethod
    def _file_metadata(
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> tuple[str | None, str | None, str | None, str | None]:
        if tool_name not in {"write_file", "edit_file"}:
            return None, None, None, None
        raw_path = arguments.get("file_path")
        if not isinstance(raw_path, str):
            return None, None, None, None
        path = Path(raw_path).expanduser().resolve()
        pre_hash = _file_hash(path)
        expected_hash = None
        if tool_name == "write_file" and isinstance(arguments.get("content"), str):
            written = arguments["content"].replace("\n", os.linesep)
            expected_hash = _sha256_bytes(written.encode("utf-8"))
        elif tool_name == "edit_file" and path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = None
            old = arguments.get("old_string")
            new = arguments.get("new_string")
            if (
                content is not None
                and isinstance(old, str)
                and isinstance(new, str)
                and content.count(old) == 1
            ):
                expected_hash = _sha256_bytes(
                    content.replace(old, new, 1).replace("\n", os.linesep).encode("utf-8")
                )
        checkpoint_ref = f"preimage:{pre_hash or 'missing'}"
        return str(path), pre_hash, expected_hash, checkpoint_ref

    def mark_tool_started(
        self,
        state,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        *,
        model: str | None = None,
        trace_id: str | None = None,
        trace_ref: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            now = _utc_now()
            cursor = connection.execute(
                """
                UPDATE tool_calls SET status=?, attempt=attempt+1,
                    started_at=?, updated_at=?
                WHERE run_id=? AND tool_call_id=? AND status IN (?, ?, ?)
                """,
                (
                    ToolCallStatus.STARTED.value,
                    now,
                    now,
                    state.run_id,
                    tool_call_id,
                    ToolCallStatus.PLANNED.value,
                    ToolCallStatus.UNKNOWN.value,
                    ToolCallStatus.FAILED.value,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM tool_calls WHERE run_id=? AND tool_call_id=?",
                    (state.run_id, tool_call_id),
                ).fetchone()
                status = row[0] if row else "missing"
                raise RunStoreError(
                    f"Cannot start tool call {tool_call_id}: persisted status is {status}."
                )
            self._save_boundary(
                connection,
                state,
                messages,
                "before_tool",
                model=model,
                trace_id=trace_id,
                trace_ref=trace_ref,
            )

    def mark_tool_result(
        self,
        state,
        messages: list[dict[str, Any]],
        result: ToolResult,
        *,
        model: str | None = None,
        trace_id: str | None = None,
        trace_ref: str | None = None,
    ) -> None:
        status = ToolCallStatus.SUCCEEDED if result.ok else ToolCallStatus.FAILED
        summary = result_summary(result)
        summary.update({
            "call_id": result.call_id,
            "tool_name": result.tool_name,
            "safe_message": _redact(result.safe_message),
            "evidence": _redact(result.evidence),
        })
        with self._transaction() as connection:
            now = _utc_now()
            cursor = connection.execute(
                """
                UPDATE tool_calls SET status=?, result_json=?, retryable=?,
                    completed_at=?, updated_at=?
                WHERE run_id=? AND tool_call_id=?
                """,
                (
                    status.value,
                    _json(summary),
                    int(result.retryable),
                    now,
                    now,
                    state.run_id,
                    result.call_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RunStoreError(
                    f"Cannot persist tool result {result.call_id}: no planned call exists."
                )
            self._save_boundary(
                connection,
                state,
                messages,
                "after_tool_before_receipt",
                model=model,
                trace_id=trace_id,
                trace_ref=trace_ref,
            )

    def reconcile_started(self, run_id: str) -> list[StoredToolCall]:
        """Convert incomplete STARTED calls to UNKNOWN, resolving file hashes when possible."""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE run_id=? AND status=?",
                (run_id, ToolCallStatus.STARTED.value),
            ).fetchall()
            now = _utc_now()
            for row in rows:
                status = ToolCallStatus.UNKNOWN
                note = "process ended after execution started but before a durable result"
                if row["target_path"] and row["expected_hash"]:
                    target = Path(row["target_path"])
                    current = _file_hash(target)
                    if current == row["expected_hash"]:
                        status = ToolCallStatus.SUCCEEDED
                        note = "target file hash confirms the intended state"
                    elif current == row["pre_hash"]:
                        from .checkpoints import has_preimage

                        if has_preimage(target, row["pre_hash"]):
                            note = (
                                "legacy checkpoint and file hash confirm the target "
                                "remains at its pre-call state"
                            )
                connection.execute(
                    """
                    UPDATE tool_calls SET status=?, recovery_note=?, updated_at=?
                    WHERE run_id=? AND tool_call_id=?
                    """,
                    (status.value, note, now, run_id, row["tool_call_id"]),
                )
        return self.get_tool_calls(run_id)

    def get_tool_calls(self, run_id: str) -> list[StoredToolCall]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_calls WHERE run_id=? ORDER BY planned_at, rowid",
                (run_id,),
            ).fetchall()
        return [self._tool_call_from_row(row) for row in rows]

    @staticmethod
    def _tool_call_from_row(row: sqlite3.Row) -> StoredToolCall:
        try:
            if int(row["schema_version"]) != SCHEMA_VERSION:
                raise RunStoreVersionError(
                    f"Tool-call schema {row['schema_version']} is incompatible with "
                    f"supported schema {SCHEMA_VERSION}."
                )
            return StoredToolCall(
                run_id=row["run_id"],
                tool_call_id=row["tool_call_id"],
                logical_step=row["logical_step"],
                tool_name=row["tool_name"],
                tool_version=row["tool_version"],
                side_effect=row["side_effect"],
                idempotent=bool(row["idempotent"]),
                status=ToolCallStatus(row["status"]),
                idempotency_key=row["idempotency_key"],
                arguments=_load_json(row["arguments_json"], {}),
                replay_arguments=_load_json(row["replay_arguments_json"], None),
                result=_load_json(row["result_json"], None),
                retryable=bool(row["retryable"]),
                attempt=int(row["attempt"]),
                max_attempts=int(row["max_attempts"]),
                target_path=row["target_path"],
                pre_hash=row["pre_hash"],
                expected_hash=row["expected_hash"],
                checkpoint_ref=row["checkpoint_ref"],
                recovery_note=row["recovery_note"],
            )
        except RunStoreError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise RunStoreCorruptError(
                "Run store contains an invalid tool-call record; preserve it for "
                "inspection and recover from a known-good copy."
            ) from exc

    def load_run(self, run_id: str) -> StoredRun:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(f"Run '{run_id}' was not found in {self.path}.")
        if int(row["schema_version"]) != SCHEMA_VERSION:
            raise RunStoreVersionError(
                f"Run {run_id} uses incompatible schema {row['schema_version']}."
            )
        return StoredRun(
            run_id=run_id,
            state=_load_json(row["state_json"], {}),
            messages=_load_json(row["messages_json"], []),
            model=row["model"],
            trace_ref=row["trace_ref"],
            updated_at=row["updated_at"],
        )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT run_id, status, current_step, round, state_version,
                       goal, exit_reason, updated_at
                FROM runs ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_status(self, run_id: str) -> dict[str, Any]:
        run = self.load_run(run_id)
        calls = self.get_tool_calls(run_id)
        counts = {status.value: 0 for status in ToolCallStatus}
        for call in calls:
            counts[call.status.value] += 1
        recovery_required = [
            {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "status": call.status.value,
                "recovery_note": call.recovery_note,
            }
            for call in calls
            if call.status is ToolCallStatus.UNKNOWN
        ]
        return {
            "run_id": run_id,
            "status": run.state.get("status"),
            "current_step": run.state.get("current_step"),
            "round": run.state.get("round"),
            "state_version": run.state.get("state_version"),
            "exit_reason": run.state.get("exit_reason"),
            "tool_calls": counts,
            "recovery_required": recovery_required,
            "updated_at": run.updated_at,
        }

