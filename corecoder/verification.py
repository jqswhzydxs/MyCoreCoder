"""Pluggable, evidence-producing verification for completed Agent runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class VerificationStatus(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ERROR = "error"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """One content-safe observation supporting or rejecting a criterion."""

    evidence_id: str
    verifier: str
    kind: str
    source: str
    criterion: str
    passed: bool
    summary: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        verifier: str,
        kind: str,
        source: str,
        criterion: str,
        passed: bool,
        summary: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> VerificationEvidence:
        safe_metadata = _json_safe(dict(metadata or {}))
        identity = json.dumps(
            {
                "verifier": verifier,
                "kind": kind,
                "source": source,
                "criterion": criterion,
                "passed": passed,
                "metadata": safe_metadata,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            evidence_id=f"verify:{_digest(identity)}",
            verifier=verifier,
            kind=kind,
            source=source,
            criterion=criterion,
            passed=passed,
            summary=summary,
            metadata=safe_metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "verifier": self.verifier,
            "kind": self.kind,
            "source": self.source,
            "criterion": self.criterion,
            "passed": self.passed,
            "summary": self.summary,
            "metadata": _json_safe(self.metadata),
        }

    def trace_dict(self) -> dict[str, Any]:
        """Metadata-only trace form: no path, command, criterion, or content."""
        return {
            "evidence_id": self.evidence_id,
            "verifier": self.verifier,
            "kind": self.kind,
            "source_sha256": _digest(self.source),
            "criterion_sha256": _digest(self.criterion),
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class VerificationCommandResult:
    """Content-free observation returned by the permission-aware runtime."""

    authorized: bool
    exit_code: int | None = None
    timed_out: bool = False
    error_category: str | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0


CommandRunner = Callable[[str, float | None, Path | None], VerificationCommandResult]


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    run_id: str
    goal: str
    success_criteria: tuple[str, ...]
    model_text: str
    completed_calls: tuple[Mapping[str, Any], ...]
    pending_calls: tuple[Mapping[str, Any], ...]
    evidence_refs: tuple[str, ...]
    state_version: int
    command_runner: CommandRunner | None = field(default=None, repr=False, compare=False)

    def legacy_snapshot(self) -> dict[str, Any]:
        """Copy-only compatibility view for pre-protocol callable verifiers."""
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "status": "verifying",
            "current_step": "verifying",
            "completed_calls": [dict(call) for call in self.completed_calls],
            "pending_calls": [dict(call) for call in self.pending_calls],
            "evidence_refs": list(self.evidence_refs),
            "state_version": self.state_version,
            "final_text": self.model_text,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    verifier: str
    evidence: tuple[VerificationEvidence, ...] = ()
    failed_criteria: tuple[str, ...] = ()
    verifier_names: tuple[str, ...] = ()
    error_category: str | None = None

    def __post_init__(self) -> None:
        if self.status is VerificationStatus.PASSED:
            if not self.evidence:
                raise ValueError("passed verification requires external evidence")
            if any(not item.passed for item in self.evidence):
                raise ValueError("passed verification cannot contain failed evidence")

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "verifier": self.verifier,
            "verifier_names": list(self.verifier_names or (self.verifier,)),
            "evidence": [item.to_dict() for item in self.evidence],
            "failed_criteria": list(self.failed_criteria),
            "error_category": self.error_category,
        }


@runtime_checkable
class Verifier(Protocol):
    name: str

    def verify(self, request: VerificationRequest) -> VerificationResult:
        ...


class CallableVerifierAdapter:
    """Compatibility adapter for the original ``snapshot -> bool`` hook."""

    name = "trusted_callable"

    def __init__(self, callback: Callable[[dict[str, Any]], Any]):
        self.callback = callback

    def verify(self, request: VerificationRequest) -> VerificationResult:
        raw = self.callback(request.legacy_snapshot())
        if isinstance(raw, VerificationResult):
            return raw
        passed = bool(raw)
        criterion = "trusted callable verifier approves"
        evidence = VerificationEvidence.create(
            verifier=self.name,
            kind="trusted_callback",
            source=getattr(self.callback, "__qualname__", type(self.callback).__qualname__),
            criterion=criterion,
            passed=passed,
            summary="Trusted callable returned a boolean verification decision.",
            metadata={"decision": passed},
        )
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            verifier=self.name,
            evidence=(evidence,),
            failed_criteria=() if passed else (criterion,),
            verifier_names=(self.name,),
        )


class CompositeVerifier:
    name = "composite"

    def __init__(self, verifiers: list[Verifier] | tuple[Verifier, ...]):
        self.verifiers = tuple(verifiers)
        if not self.verifiers:
            raise ValueError("CompositeVerifier requires at least one verifier")

    def verify(self, request: VerificationRequest) -> VerificationResult:
        results: list[VerificationResult] = []
        for verifier in self.verifiers:
            try:
                result = verifier.verify(request)
            except Exception:  # noqa: BLE001 - verifier internals remain controlled
                result = VerificationResult(
                    status=VerificationStatus.ERROR,
                    verifier=getattr(verifier, "name", type(verifier).__name__),
                    failed_criteria=("verifier completed without an internal error",),
                    error_category="verifier_exception",
                )
            results.append(result)

        evidence = tuple(item for result in results for item in result.evidence)
        failed = tuple(
            dict.fromkeys(item for result in results for item in result.failed_criteria)
        )
        statuses = {result.status for result in results}
        if VerificationStatus.ERROR in statuses:
            status = VerificationStatus.ERROR
        elif VerificationStatus.FAILED in statuses:
            status = VerificationStatus.FAILED
        elif VerificationStatus.INSUFFICIENT_EVIDENCE in statuses:
            status = VerificationStatus.INSUFFICIENT_EVIDENCE
        elif statuses == {VerificationStatus.PASSED} and evidence:
            status = VerificationStatus.PASSED
        else:
            status = VerificationStatus.INSUFFICIENT_EVIDENCE
            failed = failed or ("at least one external verification evidence item exists",)
        return VerificationResult(
            status=status,
            verifier=self.name,
            evidence=evidence,
            failed_criteria=failed,
            verifier_names=tuple(
                name
                for result in results
                for name in (result.verifier_names or (result.verifier,))
            ),
            error_category=(
                next(
                    (result.error_category for result in results if result.error_category),
                    None,
                )
                if status is VerificationStatus.ERROR
                else None
            ),
        )


def _file_snapshot(path: Path) -> dict[str, Any]:
    exists = path.exists()
    snapshot: dict[str, Any] = {"exists": exists, "is_file": path.is_file() if exists else False}
    if exists and path.is_file():
        try:
            payload = path.read_bytes()
        except OSError:
            snapshot["readable"] = False
        else:
            snapshot.update({
                "readable": True,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            })
    return snapshot


class FileStateVerifier:
    name = "file_state"

    def __init__(
        self,
        path: str | Path,
        *,
        must_exist: bool = True,
        content_contains: str | None = None,
        content_equals: str | None = None,
        expected_sha256: str | None = None,
        must_remain_unchanged: bool = False,
        criterion: str | None = None,
    ):
        if not must_exist and any(
            value is not None for value in (content_contains, content_equals, expected_sha256)
        ):
            raise ValueError("content constraints require must_exist=True")
        self.path = Path(path).expanduser().resolve()
        self.must_exist = must_exist
        self.content_contains = content_contains
        self.content_equals = content_equals
        self.expected_sha256 = expected_sha256
        self.must_remain_unchanged = must_remain_unchanged
        self.criterion = criterion or f"configured file state holds for {self.path.name}"
        self._baseline = _file_snapshot(self.path) if must_remain_unchanged else None

    def verify(self, request: VerificationRequest) -> VerificationResult:
        actual = _file_snapshot(self.path)
        checks = [actual["exists"] is self.must_exist]
        if self.must_exist and actual.get("is_file"):
            if self.content_contains is not None or self.content_equals is not None:
                try:
                    content = self.path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    checks.append(False)
                else:
                    if self.content_contains is not None:
                        checks.append(self.content_contains in content)
                    if self.content_equals is not None:
                        checks.append(content == self.content_equals)
            if self.expected_sha256 is not None:
                checks.append(actual.get("sha256") == self.expected_sha256)
        elif self.must_exist:
            checks.append(False)
        if self._baseline is not None:
            checks.append(actual == self._baseline)
        passed = all(checks)
        metadata = {
            "actual": actual,
            "must_exist": self.must_exist,
            "content_constraint": (
                "equals"
                if self.content_equals is not None
                else "contains" if self.content_contains is not None else None
            ),
            "expected_sha256_configured": self.expected_sha256 is not None,
            "unchanged_check": self.must_remain_unchanged,
        }
        evidence = VerificationEvidence.create(
            verifier=self.name,
            kind="file_state",
            source=str(self.path),
            criterion=self.criterion,
            passed=passed,
            summary="Configured file-state conditions passed." if passed else "File-state conditions failed.",
            metadata=metadata,
        )
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            verifier=self.name,
            evidence=(evidence,),
            failed_criteria=() if passed else (self.criterion,),
            verifier_names=(self.name,),
        )


class CommandExitCodeVerifier:
    name = "command_exit_code"
    evidence_kind = "command_exit_code"

    def __init__(
        self,
        command: str,
        *,
        expected_exit_codes: tuple[int, ...] = (0,),
        timeout_seconds: float | None = 120,
        cwd: str | Path | None = None,
        criterion: str = "configured verification command exits successfully",
    ):
        if not isinstance(command, str) or not command.strip():
            raise ValueError("verification command must be a non-empty caller-provided string")
        if not expected_exit_codes:
            raise ValueError("expected_exit_codes must not be empty")
        self.command = command
        self.expected_exit_codes = tuple(int(code) for code in expected_exit_codes)
        self.timeout_seconds = timeout_seconds
        self.cwd = Path(cwd).expanduser().resolve() if cwd is not None else None
        self.criterion = criterion

    def verify(self, request: VerificationRequest) -> VerificationResult:
        if request.command_runner is None:
            return VerificationResult(
                status=VerificationStatus.INSUFFICIENT_EVIDENCE,
                verifier=self.name,
                failed_criteria=(self.criterion,),
                verifier_names=(self.name,),
                error_category="command_runner_unavailable",
            )
        observation = request.command_runner(
            self.command,
            self.timeout_seconds,
            self.cwd,
        )
        passed = (
            observation.authorized
            and not observation.timed_out
            and observation.error_category is None
            and observation.exit_code in self.expected_exit_codes
        )
        evidence = VerificationEvidence.create(
            verifier=self.name,
            kind=self.evidence_kind,
            source=f"trusted-command:{_digest(self.command)}",
            criterion=self.criterion,
            passed=passed,
            summary=(
                "Configured command produced an accepted exit code."
                if passed
                else "Configured command did not satisfy its exit-code condition."
            ),
            metadata={
                "authorized": observation.authorized,
                "exit_code": observation.exit_code,
                "expected_exit_codes": self.expected_exit_codes,
                "timed_out": observation.timed_out,
                "error_category": observation.error_category,
                "stdout_sha256": observation.stdout_sha256,
                "stderr_sha256": observation.stderr_sha256,
                "stdout_bytes": observation.stdout_bytes,
                "stderr_bytes": observation.stderr_bytes,
            },
        )
        status = (
            VerificationStatus.ERROR
            if observation.error_category not in {None, "permission_denied", "timeout"}
            else VerificationStatus.PASSED if passed else VerificationStatus.FAILED
        )
        return VerificationResult(
            status=status,
            verifier=self.name,
            evidence=(evidence,),
            failed_criteria=() if passed else (self.criterion,),
            verifier_names=(self.name,),
            error_category=observation.error_category if status is VerificationStatus.ERROR else None,
        )


class TestResultVerifier(CommandExitCodeVerifier):
    name = "test_result"
    evidence_kind = "test_result"

    def __init__(
        self,
        command: str,
        *,
        timeout_seconds: float | None = 300,
        cwd: str | Path | None = None,
        criterion: str = "configured test command passes",
    ):
        super().__init__(
            command,
            expected_exit_codes=(0,),
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            criterion=criterion,
        )


class NoFailedToolCallVerifier:
    name = "no_failed_tool_call"

    def __init__(self, criterion: str = "no unresolved failed tool call remains"):
        self.criterion = criterion

    def verify(self, request: VerificationRequest) -> VerificationResult:
        failed = [call for call in request.completed_calls if call.get("outcome") != "success"]
        passed = not failed and not request.pending_calls
        evidence = VerificationEvidence.create(
            verifier=self.name,
            kind="tool_call_state",
            source=f"run:{request.run_id}",
            criterion=self.criterion,
            passed=passed,
            summary="Tool-call state has no unresolved failures." if passed else "Failed or pending tool calls remain.",
            metadata={
                "completed_count": len(request.completed_calls),
                "failed_count": len(failed),
                "pending_count": len(request.pending_calls),
                "failed_categories": sorted({
                    str(call.get("error_category") or "unknown") for call in failed
                }),
            },
        )
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            verifier=self.name,
            evidence=(evidence,),
            failed_criteria=() if passed else (self.criterion,),
            verifier_names=(self.name,),
        )


class RequiredEvidenceVerifier:
    name = "required_evidence"

    def __init__(self, required_refs: list[str] | tuple[str, ...]):
        self.required_refs = tuple(dict.fromkeys(str(item) for item in required_refs))
        if not self.required_refs:
            raise ValueError("RequiredEvidenceVerifier requires at least one evidence reference")

    def verify(self, request: VerificationRequest) -> VerificationResult:
        available = frozenset(request.evidence_refs)
        missing = tuple(item for item in self.required_refs if item not in available)
        evidence = tuple(
            VerificationEvidence.create(
                verifier=self.name,
                kind="evidence_reference",
                source=reference,
                criterion=f"required evidence reference {_digest(reference)[:12]} exists",
                passed=reference in available,
                summary=(
                    "Required evidence reference is present."
                    if reference in available
                    else "Required evidence reference is missing."
                ),
                metadata={"reference_sha256": _digest(reference)},
            )
            for reference in self.required_refs
        )
        status = (
            VerificationStatus.PASSED
            if not missing
            else VerificationStatus.INSUFFICIENT_EVIDENCE
        )
        return VerificationResult(
            status=status,
            verifier=self.name,
            evidence=evidence,
            failed_criteria=tuple(
                f"required evidence {_digest(reference)[:12]} is present" for reference in missing
            ),
            verifier_names=(self.name,),
        )
