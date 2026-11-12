"""Offline reliability demo: crash after an edit, resume, and verify once.

The scripted model chooses deterministic actions, but it does not decide whether
the task succeeded.  The demo exits non-zero unless the real file state, pytest
exit code, trace chain, and exactly-once edit count all agree.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from corecoder import (
    Agent,
    CompositeVerifier,
    FileStateVerifier,
    NoFailedToolCallVerifier,
    RunStatus,
    RunStore,
    TestResultVerifier,
    checkpoints,
)
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.run_store import InjectedProcessCrash
from corecoder.tools.edit import EditFileTool
from corecoder.tools.read import ReadFileTool
from corecoder.trace import JsonlTraceSink
from corecoder.trace_replay import load_trace, render, summarize

BUGGY_SOURCE = """def add(left, right):
    \"\"\"Return the sum of two numbers.\"\"\"
    return left - right
"""

FIXED_SOURCE = """def add(left, right):
    \"\"\"Return the sum of two numbers.\"\"\"
    return left + right
"""

TEST_SOURCE = """from calculator import add


def test_add():
    assert add(2, 3) == 5
    assert add(-2, 1) == -1
"""


@dataclass(frozen=True, slots=True)
class DemoOutcome:
    workspace: Path
    trace_file: Path
    run_id: str
    status: RunStatus
    edit_executions: int
    test_exit_code: int
    event_types: tuple[str, ...]


class CountingEditFileTool(EditFileTool):
    """Demo-only counter proving that resume did not repeat the edit."""

    def __init__(self) -> None:
        self.executions = 0

    def execute(self, file_path: str, old_string: str, new_string: str) -> str:
        self.executions += 1
        return super().execute(file_path, old_string, new_string)


class CrashOnce:
    def __init__(self, boundary: str, *, occurrence: int = 1):
        self.boundary = boundary
        self.occurrence = occurrence
        self.seen = 0
        self.fired = False

    def __call__(self, boundary: str, context: dict) -> None:
        if boundary != self.boundary or self.fired:
            return
        self.seen += 1
        if self.seen == self.occurrence:
            self.fired = True
            raise InjectedProcessCrash(
                f"offline demo crash at {boundary} for run {context['run_id']}"
            )


def _prepare_workspace(path: Path) -> tuple[Path, Path]:
    path = path.expanduser().resolve()
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"demo workspace must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    source = path / "calculator.py"
    test_file = path / "test_calculator.py"
    source.write_text(BUGGY_SOURCE, encoding="utf-8")
    test_file.write_text(TEST_SOURCE, encoding="utf-8")
    return source, test_file


def _script(source: Path) -> list[LLMResponse]:
    return [
        LLMResponse(
            content="Inspecting the implementation before editing it.",
            tool_calls=[
                ToolCall(
                    id="read-calculator",
                    name="read_file",
                    arguments={"file_path": str(source)},
                )
            ],
        ),
        LLMResponse(
            content="Replacing the incorrect subtraction with addition.",
            tool_calls=[
                ToolCall(
                    id="edit-calculator",
                    name="edit_file",
                    arguments={
                        "file_path": str(source),
                        "old_string": "return left - right",
                        "new_string": "return left + right",
                    },
                )
            ],
        ),
        LLMResponse(content="Patch applied; awaiting caller-configured verification."),
    ]


def _test_command(test_file: Path) -> str:
    executable = Path(sys.executable).resolve().as_posix()
    return f'"{executable}" -m pytest "{test_file.as_posix()}" -q'


def run_demo(workspace: Path | None = None, *, show_trace: bool = True) -> DemoOutcome:
    workspace = workspace or Path(tempfile.mkdtemp(prefix="reliablecoder-demo-"))
    source, test_file = _prepare_workspace(workspace)
    trace_file = workspace / "run.trace.jsonl"
    run_store = RunStore(workspace / "runs.db")
    trace_sink = JsonlTraceSink(trace_file)
    edit_tool = CountingEditFileTool()
    tools = [ReadFileTool(), edit_tool]
    llm = ScriptedLLM(_script(source), model="scripted-reliability-demo")

    old_checkpoint_file = checkpoints.CHECKPOINTS_FILE
    checkpoints.CHECKPOINTS_FILE = workspace / "checkpoints.json"
    checkpoints.clear()
    try:
        first = Agent(
            llm=llm,
            tools=tools,
            permission=Permission(allow_all=True),
            trace_sink=trace_sink,
            run_store=run_store,
            crash_injector=CrashOnce(
                "tool_after_execute_before_receipt",
                occurrence=2,
            ),
        )
        try:
            first.run("Fix calculator.add, then prove it with the configured tests.")
        except InjectedProcessCrash as exc:
            print(f"[1/4] Simulated crash captured: {exc}")
        else:
            raise AssertionError("crash injection did not fire")

        run_id = first.last_run_id
        if run_id is None:
            raise AssertionError("crashed run did not persist a run id")
        if source.read_text(encoding="utf-8") != FIXED_SOURCE:
            raise AssertionError("the real edit did not produce the expected file")
        print(f"[2/4] Real file changed; resuming persisted run {run_id}")

        verifier = CompositeVerifier(
            [
                FileStateVerifier(
                    source,
                    content_equals=FIXED_SOURCE,
                    criterion="calculator.py contains the intended addition fix",
                ),
                TestResultVerifier(
                    _test_command(test_file),
                    cwd=workspace,
                    criterion="offline calculator tests pass",
                ),
                NoFailedToolCallVerifier(),
            ]
        )
        resumed = Agent(
            llm=llm,
            tools=tools,
            permission=Permission(allow_all=True),
            trace_sink=trace_sink,
            run_store=run_store,
        ).resume_run(run_id, verifier=verifier)

        test_evidence = next(
            (item for item in resumed.evidence if item.get("kind") == "test_result"),
            None,
        )
        test_exit_code = (
            test_evidence.get("metadata", {}).get("exit_code")
            if test_evidence is not None
            else None
        )
        if resumed.status is not RunStatus.VERIFIED:
            raise AssertionError(f"expected VERIFIED, got {resumed.status.value}")
        if test_exit_code != 0:
            raise AssertionError(f"real pytest exit code was {test_exit_code!r}, not 0")
        if edit_tool.executions != 1:
            raise AssertionError(
                f"edit executed {edit_tool.executions} times; expected exactly once"
            )
        print("[3/4] Resume did not repeat the edit; real pytest exit code: 0")

        events = load_trace(trace_file)
        event_types = tuple(str(event.get("event_type")) for event in events)
        required_events = {"tool_started", "verification_finished", "run_finished"}
        missing = required_events.difference(event_types)
        if missing:
            raise AssertionError(f"trace is missing required events: {sorted(missing)}")
        print("[4/4] VERIFIED from external evidence; replaying the trace:\n")
        if show_trace:
            print(render(summarize(events)), end="")

        return DemoOutcome(
            workspace=workspace,
            trace_file=trace_file,
            run_id=run_id,
            status=resumed.status,
            edit_executions=edit_tool.executions,
            test_exit_code=test_exit_code,
            event_types=event_types,
        )
    finally:
        checkpoints.clear()
        checkpoints.CHECKPOINTS_FILE = old_checkpoint_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        help="Empty directory to keep demo files in (default: a new temp directory).",
    )
    parser.add_argument(
        "--no-trace-output",
        action="store_true",
        help="Still validate and save Trace, but do not print its replay.",
    )
    args = parser.parse_args(argv)
    outcome = run_demo(args.workspace, show_trace=not args.no_trace_output)
    print(f"Demo workspace: {outcome.workspace}")
    print(f"Trace JSONL: {outcome.trace_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
