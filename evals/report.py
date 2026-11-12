"""Stable JSON and Markdown reports for offline Agent evaluations."""

from __future__ import annotations

import json
from pathlib import Path

METRIC_DESCRIPTIONS = {
    "task_success_rate": "Cases satisfying their externally declared task outcome / observations",
    "verified_completion_rate": "Verifier-passed runs / observations",
    "false_completion_rate": "Completion claims missing required external evidence / completion claims",
    "duplicate_side_effect_rate": "Repeated identical mutating executions / mutating executions",
    "permission_bypass_rate": "Blocked scenarios that executed a mutation / permission checks",
    "crash_resume_rate": "Injected crashes safely resumed / injected crashes",
    "average_rounds": "Mean model rounds per observation",
    "p95_rounds": "Nearest-rank P95 model rounds",
    "average_latency_ms": "Mean deterministic simulated latency",
    "p95_latency_ms": "Nearest-rank P95 deterministic simulated latency",
    "prompt_tokens": "Total scripted prompt tokens",
    "completion_tokens": "Total scripted completion tokens",
    "estimated_cost_per_success": "Synthetic fixed-rate cost / successful tasks",
}


def json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def markdown_text(payload: dict) -> str:
    metadata = payload["metadata"]
    metrics = payload["metrics"]
    variant = metadata["variant"]
    lines = [
        f"# CoreCoder Offline Agent Eval: {variant}",
        "",
        f"- Git commit: `{metadata['git_commit']}`",
        f"- Variant: `{variant}` — {metadata['variant_definition']}",
        f"- Suite/runs: `{metadata['suite']}` / {metadata['run_count']}",
        f"- Cases/observations: {metadata['case_count']} / {metadata['observation_count']}",
        f"- Case manifest: `{metadata['case_manifest_sha256']}`",
        f"- Harness: `{metadata['harness_version']}` / case schema `{metadata['case_schema_version']}`",
        f"- Model: `{metadata['model']['type']}` (`{metadata['model']['name']}`, offline)",
        (
            f"- Prompt/version: `{metadata['prompt']['version']}` "
            f"(`{metadata['prompt']['sha256']}`)"
        ),
        (
            f"- Environment: {metadata['environment']['implementation']} "
            f"{metadata['environment']['python']} on `{metadata['environment']['platform']}`"
        ),
        "",
        "## Metrics",
        "",
        "| Metric | Value | Definition |",
        "|---|---:|---|",
    ]
    for name, description in METRIC_DESCRIPTIONS.items():
        lines.append(f"| `{name}` | {metrics[name]} | {description} |")

    lines += ["", "## Scenario slices", ""]
    lines += [
        "| Scenario | N | Success | Verified | False completion | Duplicate side effect | Permission bypass | Crash resume | Avg/P95 rounds | Avg/P95 latency ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in metrics["slices"]["scenario"].items():
        lines.append(
            f"| `{name}` | {item['observations']} | {item['task_success_rate']} | "
            f"{item['verified_completion_rate']} | {item['false_completion_rate']} | "
            f"{item['duplicate_side_effect_rate']} | {item['permission_bypass_rate']} | "
            f"{item['crash_resume_rate']} | {item['average_rounds']} / {item['p95_rounds']} | "
            f"{item['average_latency_ms']} / {item['p95_latency_ms']} |"
        )

    for title, key in (("Exit reasons", "exit_reason"), ("Error categories", "error_category")):
        lines += ["", f"## {title}", "", "| Value | Observations | Success rate |", "|---|---:|---:|"]
        for name, item in metrics["slices"][key].items():
            lines.append(
                f"| `{_cell(name)}` | {item['observations']} | {item['task_success_rate']} |"
            )

    lines += [
        "",
        "## Cases",
        "",
        "| Case | Scenario | Result | Exit | Verification | Rounds | Latency ms | Duplicates |",
        "|---|---|---|---|---|---:|---:|---:|",
    ]
    for case in payload["cases"]:
        lines.append(
            f"| `{_cell(case['id'])}` | `{case['category']}` | "
            f"{'PASS' if case['passed'] else 'FAIL'} | `{case['exit_reason']}` | "
            f"`{case['verification']['status']}` | {case['rounds']} | "
            f"{case['latency_ms']} | {case['duplicate_side_effects']} |"
        )

    lines += ["", "## Failure details", ""]
    failed = [case for case in payload["cases"] if not case["passed"]]
    if not failed:
        lines.append("All observations matched their declared expectations.")
    else:
        for case in failed:
            lines += [f"### `{case['id']}`", ""]
            lines.extend(f"- {_cell(failure)}" for failure in case["failures"])
            lines.append("")

    lines += ["", "## Statistical limits", ""]
    lines.extend(f"- {item}" for item in metadata["statistical_limits"])
    lines += [
        "",
        "## Reproduce",
        "",
        "```bash",
        f"python -m evals.runner --suite {metadata['suite']} --variant {variant}",
        "```",
        "",
        "The suite uses scripted responses, deterministic fault injection, and temporary directories only. It never calls a real provider.",
        "",
    ]
    return "\n".join(lines)


def write_reports(payload: dict, output_base: str | Path) -> tuple[Path, Path]:
    base = Path(output_base)
    if base.suffix in {".json", ".md"}:
        base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    json_path.write_text(json_text(payload), encoding="utf-8")
    markdown_path.write_text(markdown_text(payload), encoding="utf-8")
    return json_path, markdown_path
