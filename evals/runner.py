"""Run reproducible baseline/candidate evaluations against the real Agent loop."""

from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import platform
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, ClassVar

from corecoder import Agent, checkpoints
from corecoder import __version__ as corecoder_version
from corecoder.budget import RunBudget
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.run_store import InjectedProcessCrash, RunStore
from corecoder.tools.base import Tool
from corecoder.tools.edit import EditFileTool, _changed_files
from corecoder.tools.glob_tool import GlobTool
from corecoder.tools.grep import GrepTool
from corecoder.tools.read import ReadFileTool
from corecoder.tools.write import WriteFileTool
from corecoder.verification import (
    CompositeVerifier,
    FileStateVerifier,
    NoFailedToolCallVerifier,
    RequiredEvidenceVerifier,
    TestResultVerifier,
)

from . import CASE_SCHEMA_VERSION, HARNESS_VERSION, MODEL_NAME
from .faults import FaultInjectingLLM, FaultInjectingTool, FaultPlan, FaultTelemetry
from .metrics import aggregate
from .report import write_reports

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASE_DIR = Path(__file__).resolve().parent / "cases"
CATEGORIES = (
    "normal",
    "boundary",
    "permission",
    "malformed_tool_call",
    "transient_failure",
    "timeout",
    "crash_resume",
    "duplicate_side_effect",
    "false_completion",
    "no_progress_loop",
)
VARIANTS = ("baseline", "candidate")
REQUIRED_CASE_FIELDS = {
    "id", "category", "description", "input", "scripted_responses",
    "expected_tool_calls", "forbidden_tool_calls", "expected_file_state",
    "expected_final_outcome",
}
MAX_ROUNDS_TEXT = "(reached maximum tool-call rounds)"


class ExplodingTool(Tool):
    """Harness-only tool for exercising the tool exception boundary."""

    name = "explode"
    description = "Raise a deterministic exception for an offline eval."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}
    side_effect = "none"
    risk = "low"
    requires_confirmation = False
    parallel_safe = True

    def execute(self) -> str:
        raise RuntimeError("offline explosion")


class EvalWriteTool(Tool):
    """Non-sensitive idempotent file write used to exercise safe replay."""

    name = "eval_write"
    description = "Write a fixed marker to a path for an offline recovery eval."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    }
    side_effect = "write"
    risk = "medium"
    required_permissions = ("filesystem:write",)
    requires_confirmation = True
    idempotent = True
    parallel_safe = False
    version = "1"

    def resource_keys(self, arguments: dict) -> tuple[str, ...]:
        return (f"file:{Path(arguments['file_path']).resolve()}",)

    def execute(self, file_path: str) -> str:
        Path(file_path).write_text("replayed\n", encoding="utf-8")
        return f"Eval wrote {file_path}"


TOOL_FACTORIES = {
    "read_file": ReadFileTool,
    "write_file": WriteFileTool,
    "edit_file": EditFileTool,
    "glob": GlobTool,
    "grep": GrepTool,
    "explode": ExplodingTool,
    "eval_write": EvalWriteTool,
}


def load_cases(case_dir: str | Path = DEFAULT_CASE_DIR) -> list[dict]:
    cases: list[dict] = []
    seen_ids: set[str] = set()
    for path in sorted(Path(case_dir).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("schema") == "steerable_runtime_v1":
            continue
        entries = payload if isinstance(payload, list) else [payload]
        for position, raw_case in enumerate(entries, 1):
            if not isinstance(raw_case, dict):
                raise TypeError(f"{path.name}[{position}]: case must be an object")
            case = dict(raw_case)
            missing = sorted(REQUIRED_CASE_FIELDS - case.keys())
            if missing:
                raise ValueError(
                    f"{path.name}[{position}]: missing required fields: {', '.join(missing)}"
                )
            if case["category"] not in CATEGORIES:
                raise ValueError(
                    f"{path.name}[{position}]: unsupported category {case['category']!r}"
                )
            if case["id"] in seen_ids:
                raise ValueError(f"{path.name}: duplicate case id {case['id']!r}")
            seen_ids.add(case["id"])
            case["_source"] = path.name
            cases.append(case)
    if not cases:
        raise ValueError(f"no eval cases found in {case_dir}")
    return cases


def _variant_case(case: dict, variant: str) -> dict:
    selected = copy.deepcopy(case)
    overrides = selected.pop("expected_by_variant", {}).get(variant, {})
    selected.update(overrides)
    return selected


def _expand(value, workspace: Path):
    if isinstance(value, str):
        return value.replace("{{workspace}}", str(workspace))
    if isinstance(value, list):
        return [_expand(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, workspace) for key, item in value.items()}
    return value


def _normalise(value, workspace: Path):
    if isinstance(value, str):
        text = value.replace(str(workspace), "{{workspace}}")
        text = text.replace(workspace.as_posix(), "{{workspace}}")
        return text.replace("\\", "/") if "{{workspace}}" in text else text
    if isinstance(value, list):
        return [_normalise(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: _normalise(item, workspace) for key, item in value.items()}
    return value


def _normalise_evidence(items: tuple[dict, ...], workspace: Path) -> list[dict]:
    normalised = _normalise(list(items), workspace)
    for item in normalised:
        stable = {key: value for key, value in item.items() if key != "evidence_id"}
        payload = json.dumps(
            stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        item["evidence_id"] = f"verify:{hashlib.sha256(payload).hexdigest()}"
    return normalised


def _safe_path(workspace: Path, relative: str) -> Path:
    candidate = (workspace / relative).resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError(f"case path escapes workspace: {relative}")
    return candidate


def _responses(case: dict, workspace: Path) -> list[LLMResponse]:
    responses = []
    for turn in case["scripted_responses"]:
        calls = [
            ToolCall(
                id=call["id"],
                name=call["name"],
                arguments=_expand(call.get("arguments", {}), workspace),
            )
            for call in turn.get("tool_calls", [])
        ]
        responses.append(
            LLMResponse(
                content=turn.get("content", ""),
                tool_calls=calls,
                prompt_tokens=int(turn.get("prompt_tokens", 0)),
                completion_tokens=int(turn.get("completion_tokens", 0)),
            )
        )
    return responses


def _permission(mode: str | None):
    if mode in (None, "none"):
        return None
    if mode == "allow_all":
        return Permission(allow_all=True)
    if mode == "deny":
        return Permission(ask=lambda _name, _arguments: "deny")
    if mode == "non_interactive":
        return Permission()
    raise ValueError(f"unknown permission mode: {mode}")


def _python_exit_command(code: int) -> str:
    executable = sys.executable.replace("\\", "/")
    return f'"{executable}" -c "import sys; sys.exit({int(code)})"'


def _build_verifier(case: dict, workspace: Path):
    configured = case.get("verification", [])
    if isinstance(configured, dict):
        configured = [configured]
    verifiers = []
    for item in configured:
        verifier_type = item["type"]
        criterion = item.get("criterion")
        if verifier_type == "file_state":
            kwargs = {
                key: item[key]
                for key in (
                    "must_exist", "content_contains", "content_equals",
                    "expected_sha256", "must_remain_unchanged",
                )
                if key in item
            }
            if criterion is not None:
                kwargs["criterion"] = criterion
            verifiers.append(FileStateVerifier(_safe_path(workspace, item["path"]), **kwargs))
        elif verifier_type == "test_result":
            kwargs = {"cwd": workspace}
            if criterion is not None:
                kwargs["criterion"] = criterion
            verifiers.append(
                TestResultVerifier(_python_exit_command(item["python_exit_code"]), **kwargs)
            )
        elif verifier_type == "no_failed_tool_call":
            verifiers.append(
                NoFailedToolCallVerifier(**({"criterion": criterion} if criterion else {}))
            )
        elif verifier_type == "required_evidence":
            verifiers.append(RequiredEvidenceVerifier(item["refs"]))
        else:
            raise ValueError(f"unknown verification type: {verifier_type}")
    if not verifiers:
        return None
    return verifiers[0] if len(verifiers) == 1 else CompositeVerifier(verifiers)


def _argument_valid(call: ToolCall, tools: dict[str, Tool]) -> bool:
    tool = tools.get(call.name)
    if tool is None or not isinstance(call.arguments, dict):
        return False
    try:
        inspect.signature(tool.execute).bind(**call.arguments)
    except (TypeError, ValueError):
        return False
    return True


@contextmanager
def _isolated_tool_state(workspace: Path):
    old_checkpoint_file = checkpoints.CHECKPOINTS_FILE
    old_stack = list(checkpoints._stack)
    old_loaded = checkpoints._loaded
    old_changed_files = set(_changed_files)
    checkpoints.CHECKPOINTS_FILE = workspace / ".corecoder" / "checkpoints.json"
    checkpoints._stack.clear()
    checkpoints._loaded = True
    _changed_files.clear()
    try:
        yield
    finally:
        checkpoints.clear()
        checkpoints.CHECKPOINTS_FILE = old_checkpoint_file
        checkpoints._stack[:] = old_stack
        checkpoints._loaded = old_loaded
        _changed_files.clear()
        _changed_files.update(old_changed_files)


def _observations(agents: list[Agent]) -> dict[str, str]:
    return {
        message["tool_call_id"]: message["content"]
        for agent in agents
        for message in agent.messages
        if message.get("role") == "tool"
    }


def _check_expected_calls(
    case: dict,
    workspace: Path,
    actual_calls: list[ToolCall],
    observations: dict[str, str],
    failures: list[str],
) -> bool:
    expected = case["expected_tool_calls"]
    expected_names = [call["name"] for call in expected]
    actual_names = [call.name for call in actual_calls]
    selection_correct = expected_names == actual_names
    if not selection_correct:
        failures.append(f"tool selection: expected {expected_names}, got {actual_names}")
    if len(expected) != len(actual_calls):
        return selection_correct
    for index, (expected_call, actual_call) in enumerate(zip(expected, actual_calls), 1):
        expected_arguments = _expand(expected_call.get("arguments", {}), workspace)
        if expected_arguments != actual_call.arguments:
            failures.append(
                f"tool call {index} arguments: expected "
                f"{_normalise(expected_arguments, workspace)}, got "
                f"{_normalise(actual_call.arguments, workspace)}"
            )
        result = observations.get(actual_call.id)
        if result is None:
            if not expected_call.get("result_optional", False):
                failures.append(f"tool call {index} ({actual_call.name}) has no tool result")
            continue
        fragments = expected_call.get("result_contains", [])
        if isinstance(fragments, str):
            fragments = [fragments]
        for fragment in fragments:
            if fragment not in result:
                failures.append(
                    f"tool call {index} result missing {fragment!r}: "
                    f"{_normalise(result, workspace)!r}"
                )
    return selection_correct


def _check_file_state(case: dict, workspace: Path, failures: list[str]) -> dict:
    actual_state = {}
    for relative, expected in sorted(case["expected_file_state"].items()):
        path = _safe_path(workspace, relative)
        exists = path.exists()
        state = {"exists": exists}
        if exists and path.is_file() and "content" in expected:
            state["content"] = path.read_text(encoding="utf-8")
        actual_state[relative] = state
        if exists != expected["exists"]:
            failures.append(
                f"file {relative}: expected exists={expected['exists']}, got {exists}"
            )
        if exists and "content" in expected and state.get("content") != expected["content"]:
            failures.append(
                f"file {relative}: expected content {expected['content']!r}, "
                f"got {state.get('content')!r}"
            )
    return actual_state


def _check_final_outcome(
    case: dict,
    workspace: Path,
    final_text: str,
    verification_status: str,
    raised_category: str | None,
    failures: list[str],
) -> dict:
    expected = case["expected_final_outcome"]
    expected_exception = expected.get("exception_category")
    if raised_category != expected_exception:
        failures.append(
            f"exception category: expected {expected_exception!r}, got {raised_category!r}"
        )
    if "exact" in expected and final_text != expected["exact"]:
        failures.append(f"final outcome: expected {expected['exact']!r}, got {final_text!r}")
    fragments = expected.get("contains", [])
    if isinstance(fragments, str):
        fragments = [fragments]
    for fragment in fragments:
        if fragment.lower() not in final_text.lower():
            failures.append(f"final outcome missing {fragment!r}: {final_text!r}")
    expected_verification = expected.get("verification_status")
    if expected_verification is not None and verification_status != expected_verification:
        failures.append(
            f"verification status: expected {expected_verification!r}, "
            f"got {verification_status!r}"
        )
    max_rounds_reached = final_text == MAX_ROUNDS_TEXT
    expected_max_rounds = expected.get("max_rounds_reached", False)
    if max_rounds_reached != expected_max_rounds:
        failures.append(
            f"max_rounds_reached: expected {expected_max_rounds}, got {max_rounds_reached}"
        )
    required_evidence = expected.get("required_evidence", [])
    missing_evidence = [
        relative for relative in required_evidence
        if not _safe_path(workspace, relative).exists()
    ]
    claims_completion = expected.get("claims_completion", False)
    false_completion = bool(claims_completion and missing_evidence)
    expected_false_completion = expected.get("false_completion", False)
    if false_completion != expected_false_completion:
        failures.append(
            f"false_completion: expected {expected_false_completion}, got "
            f"{false_completion}; missing evidence={missing_evidence}"
        )
    return {
        "text": _normalise(final_text, workspace),
        "claims_completion": claims_completion,
        "required_evidence": required_evidence,
        "missing_evidence": missing_evidence,
        "false_completion": false_completion,
        "max_rounds_reached": max_rounds_reached,
        "task_success": bool(expected.get("task_success", not false_completion)),
    }


def _budget(case: dict) -> RunBudget | None:
    configured = case.get("budget")
    return RunBudget(**configured) if configured else None


def _execution_signature(item: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"tool_name": item["tool_name"], "arguments": item["arguments"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_case(case: dict, variant: str = "baseline", *, run_index: int = 1) -> dict:
    if variant not in VARIANTS:
        raise ValueError(f"unknown eval variant: {variant}")
    case = _variant_case(case, variant)
    failures: list[str] = []
    final_text = ""
    raised: str | None = None
    raised_category: str | None = None
    exit_reason = "process_crash"
    verification = {"status": "not_run", "evidence": [], "failed_criteria": []}
    telemetry = FaultTelemetry()
    plan = FaultPlan(case.get("faults"), telemetry)

    with tempfile.TemporaryDirectory(prefix=f"corecoder-eval-{case['id']}-") as temp_dir:
        workspace = Path(temp_dir).resolve()
        for relative, content in sorted(case.get("fixtures", {}).items()):
            path = _safe_path(workspace, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        raw_tools = [TOOL_FACTORIES[name]() for name in case.get("tools", [])]
        tools = [FaultInjectingTool(tool, plan, telemetry) for tool in raw_tools]
        tools_by_name = {tool.name: tool for tool in tools}
        verifier = _build_verifier(case, workspace)
        store = RunStore(workspace / ".corecoder" / "eval-runs.sqlite3")
        llms: list[FaultInjectingLLM] = []
        agents: list[Agent] = []
        result = None

        def new_llm() -> FaultInjectingLLM:
            llm = FaultInjectingLLM(_responses(case, workspace), plan, telemetry)
            llms.append(llm)
            return llm

        def new_agent(llm: FaultInjectingLLM) -> Agent:
            agent = Agent(
                llm=llm,
                tools=tools,
                max_rounds=case.get("max_rounds", 50),
                permission=_permission(case.get("permission")),
                run_store=store,
                crash_injector=plan.crash_injector,
            )
            agents.append(agent)
            return agent

        with _isolated_tool_state(workspace):
            llm = new_llm()
            agent = new_agent(llm)
            task = (
                {"goal": case["input"], "success_criteria": case.get("success_criteria", []),
                 "verifier": verifier}
                if verifier is not None else case["input"]
            )
            try:
                result = agent.run(task, budget=_budget(case))
            except InjectedProcessCrash as exc:
                if variant == "candidate":
                    resumed = new_agent(llm)
                    result = resumed.resume_run(
                        agent.last_run_id or "", verifier=verifier, budget=_budget(case)
                    )
                elif case.get("baseline_restart_after_crash", False):
                    result = new_agent(new_llm()).run(task, budget=_budget(case))
                else:
                    raised = f"{type(exc).__name__}: {exc}"
                    raised_category = type(exc).__name__
            except Exception as exc:  # noqa: BLE001 - case failure must not abort suite
                raised = f"{type(exc).__name__}: {exc}"
                raised_category = type(exc).__name__

        if result is not None:
            final_text = result.model_text
            exit_reason = result.exit_reason.value if result.exit_reason else "unknown"
            verification = {
                "status": result.verification_status.value,
                "evidence": _normalise_evidence(result.evidence, workspace),
                "failed_criteria": list(result.failed_criteria),
            }
            if result.error_type:
                raised = result.error_type
                raised_category = result.error_type

        actual_calls = [call for llm_item in llms for call in llm_item.calls]
        observations = _observations(agents)
        selection_correct = _check_expected_calls(
            case, workspace, actual_calls, observations, failures
        )
        forbidden = set(case["forbidden_tool_calls"])
        tool_calls = [
            {
                "id": call.id, "name": call.name,
                "arguments": _normalise(call.arguments, workspace),
                "argument_valid": _argument_valid(call, tools_by_name),
                "forbidden": call.name in forbidden,
                "result": _normalise(observations.get(call.id, ""), workspace),
            }
            for call in actual_calls
        ]
        for call in tool_calls:
            if call["forbidden"]:
                failures.append(f"forbidden tool requested: {call['name']}")

        file_state = _check_file_state(case, workspace, failures)
        final_outcome = _check_final_outcome(
            case, workspace, final_text, verification["status"], raised_category, failures
        )
        mutating_executions = [
            item for item in telemetry.tool_executions
            if item["side_effect"] in {"write", "external"}
        ]
        duplicate_counts = Counter(_execution_signature(item) for item in mutating_executions)
        duplicate_side_effects = sum(max(0, count - 1) for count in duplicate_counts.values())
        permission_expected_block = bool(case.get("permission_should_block", False))
        permission_bypasses = len(mutating_executions) if permission_expected_block else 0
        error_categories = sorted(
            {
                error
                for agent_item in agents
                for result_item in agent_item.tool_results
                for error in [result_item.error_category]
                if error
            }
            | ({raised_category} if raised_category else set())
        )

    passed = not failures
    task_success = bool(passed and final_outcome["task_success"])
    crash_injected = bool(telemetry.crash_events)
    crash_resumed = bool(
        variant == "candidate" and crash_injected and result is not None and raised is None
    )
    return {
        "id": case["id"], "category": case["category"],
        "description": case["description"], "source": case.get("_source", "<in-memory>"),
        "variant": variant, "run_index": run_index, "passed": passed,
        "task_success": task_success, "failures": failures,
        "rounds": sum(llm_item.rounds for llm_item in llms),
        "latency_ms": round(telemetry.latency_ms, 6),
        "prompt_tokens": telemetry.prompt_tokens,
        "completion_tokens": telemetry.completion_tokens,
        "estimated_cost_usd": round(telemetry.estimated_cost_usd, 12),
        "model_attempts": telemetry.model_attempts,
        "tool_selection_correct": selection_correct, "tool_calls": tool_calls,
        "tool_executions": len(telemetry.tool_executions),
        "side_effect_executions": len(mutating_executions),
        "duplicate_side_effects": duplicate_side_effects,
        "permission_bypasses": permission_bypasses,
        "permission_checks": 1 if permission_expected_block else 0,
        "crash_injected": crash_injected, "crash_resumed": crash_resumed,
        "injected_faults": telemetry.injected_faults, "file_state": file_state,
        "final_outcome": final_outcome, "verification": verification,
        "exit_reason": exit_reason, "error_categories": error_categories,
        "exception": raised,
    }


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not installed"


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _case_manifest(cases: list[dict]) -> str:
    stable = [{key: value for key, value in case.items() if key != "_source"} for case in cases]
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _metadata(cases: list[dict], variant: str, suite: str, runs: int) -> dict:
    prompt_path = ROOT / "corecoder" / "prompt.py"
    return {
        "harness_version": HARNESS_VERSION,
        "case_schema_version": CASE_SCHEMA_VERSION,
        "git_commit": _git_commit(),
        "variant": variant,
        "variant_definition": (
            "controlled ablation: durable journal without automatic resume"
            if variant == "baseline" else "current runtime with safe RunStore resume enabled"
        ),
        "suite": suite, "run_count": runs, "case_count": len(cases),
        "observation_count": len(cases) * runs,
        "case_manifest_sha256": _case_manifest(cases),
        "model": {"type": "FaultInjectingLLM", "name": MODEL_NAME, "offline": True},
        "model_configuration": {
            "scripted_inputs": "identical across variants",
            "max_transient_retries": 3,
            "temperature": 0,
        },
        "prompt": {
            "version": f"corecoder-{corecoder_version}",
            "source": "corecoder/prompt.py",
            "sha256": hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "packages": {
                "corecoder": corecoder_version, "openai": _package_version("openai"),
                "prompt_toolkit": _package_version("prompt_toolkit"),
                "python-dotenv": _package_version("python-dotenv"),
                "rich": _package_version("rich"),
            },
        },
        "statistical_limits": [
            "Scripted cases are deterministic and do not estimate live-model variance.",
            "Latency is simulated work-unit latency, not wall-clock performance.",
            "Cost uses fixed harness rates ($1/M prompt, $2/M completion), not provider billing.",
            "P95 is the nearest-rank percentile over the recorded case runs.",
        ],
    }


def run_suite(
    case_dir: str | Path = DEFAULT_CASE_DIR,
    *, suite: str = "all", variant: str = "baseline", runs: int = 1,
) -> dict:
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}")
    if runs < 1:
        raise ValueError("runs must be at least 1")
    cases = load_cases(case_dir)
    if suite != "all":
        if suite not in CATEGORIES:
            raise ValueError(f"suite must be 'all' or one of {CATEGORIES}")
        cases = [case for case in cases if case["category"] == suite]
    results = [
        run_case(case, variant, run_index=run_index)
        for run_index in range(1, runs + 1) for case in cases
    ]
    return {
        "schema_version": "2", "metadata": _metadata(cases, variant, suite, runs),
        "metrics": aggregate(results), "cases": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", default=str(DEFAULT_CASE_DIR), help="Directory containing case JSON files"
    )
    parser.add_argument("--suite", default="all", help="all or one scenario category")
    parser.add_argument("--variant", choices=VARIANTS, default="baseline")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--output", help="Output path without extension")
    args = parser.parse_args(argv)
    output = args.output or f"evals/results/{args.variant}"
    payload = run_suite(args.cases, suite=args.suite, variant=args.variant, runs=args.runs)
    json_path, markdown_path = write_reports(payload, output)
    counts = payload["metrics"]["counts"]
    print(
        f"Offline eval ({args.variant}): {counts['passed_cases']}/"
        f"{counts['observations']} observations matched expectations"
    )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    failed = [case for case in payload["cases"] if not case["passed"]]
    for case in failed:
        print(f"FAIL {case['id']} [{case['category']}]")
        for failure in case["failures"]:
            print(f"  - {failure}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
