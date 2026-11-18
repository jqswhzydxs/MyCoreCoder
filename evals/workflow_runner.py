"""Execute the fixed recovery contracts; this is not a runtime ablation study."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/cases/workflows/recovery.json"


def run_evaluation() -> dict:
    dataset = json.loads(CASES.read_text(encoding="utf-8"))
    assert len(dataset["cases"]) == 12 and len({case["id"] for case in dataset["cases"]}) == 12
    (ROOT / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="graph-contract-", dir=ROOT / ".tmp") as directory:
        junit = Path(directory) / "results.xml"
        command = [sys.executable, "-m", "pytest", *(case["pytest_node"] for case in dataset["cases"]),
                   "-q", "-p", "no:cacheprovider", "--junitxml=" + str(junit), "--basetemp=" + str(Path(directory) / "cases")]
        environment = dict(os.environ, CORECODER_REQUIRE_GRAPH_TESTS="1", CORECODER_REQUIRE_KNOWLEDGE_TESTS="1",
                           LANGSMITH_TRACING="false", ANONYMIZED_TELEMETRY="False")
        start = time.monotonic()
        process = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=180, check=False)
        elapsed = time.monotonic() - start
        elements = ET.parse(junit).getroot().findall(".//testcase") if junit.exists() else []
        observed = {item.attrib["name"]: item for item in elements}
        rows = []
        for case in dataset["cases"]:
            name = case["pytest_node"].split("::")[-1]
            item = observed.get(name)
            passed = item is not None and not any(item.find(tag) is not None for tag in ("failure", "error", "skipped"))
            rows.append({**case, "passed": passed, "elapsed_seconds": float(item.attrib.get("time", 0)) if item is not None else None})
    sources = sorted((ROOT / "corecoder/workflows").glob("*.py")) + [ROOT / "corecoder/agent.py", ROOT / "corecoder/run_store.py",
        ROOT / "corecoder/integrations/agent_llm_bridge.py", ROOT / "tests/workflows/test_graph.py", CASES, Path(__file__)]
    return {"mode": "offline_contract", "python": platform.python_version(), "platform": platform.system(),
            "dependencies": {name: version(name) for name in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite")},
            "limitations": "Fixed fault/control contracts with actual StateGraph, SQLite and tool effects. No live model quality or matched runtime/graph performance improvement is claimed.",
            "passed": sum(row["passed"] for row in rows), "total": len(rows), "pytest_exit_code": process.returncode,
            "elapsed_seconds": elapsed, "dataset_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
            "source_manifest": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
            "cases": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/results/next-stage/workflow-contracts.json")
    args = parser.parse_args(argv)
    report = run_evaluation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Graph recovery contracts", "", report["limitations"], "",
             f"Passed: {report['passed']}/{report['total']}; pytest exit: {report['pytest_exit_code']}", "",
             "| Scenario | Passed |", "|---|---|"]
    lines.extend(f"| {case['id']} | {case['passed']} |" for case in report["cases"])
    args.output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return int(report["pytest_exit_code"] != 0 or report["passed"] != report["total"])


if __name__ == "__main__":
    raise SystemExit(main())
