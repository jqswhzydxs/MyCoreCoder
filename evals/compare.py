"""Compare matched baseline/candidate reports and enforce safety gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = Path(__file__).resolve().parent / "results"
DEFAULT_GATES = Path(__file__).resolve().parent / "gates.json"

COMPARISON_METRICS = (
    "task_success_rate",
    "verified_completion_rate",
    "false_completion_rate",
    "duplicate_side_effect_rate",
    "permission_bypass_rate",
    "crash_resume_rate",
    "average_rounds",
    "p95_rounds",
    "average_latency_ms",
    "p95_latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "estimated_cost_per_success",
)
LOWER_IS_BETTER = frozenset({
    "false_completion_rate", "duplicate_side_effect_rate", "permission_bypass_rate",
    "average_rounds", "p95_rounds", "average_latency_ms", "p95_latency_ms",
    "prompt_tokens", "completion_tokens", "estimated_cost_per_success",
})
SAFETY_METRICS = frozenset({
    "false_completion_rate", "duplicate_side_effect_rate", "permission_bypass_rate",
})


def _resolve_report(reference: str | Path) -> Path:
    path = Path(reference)
    candidates = [path, path.with_suffix(".json"), DEFAULT_RESULTS / f"{path.name}.json"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"eval report not found: {reference}")


def load_report(reference: str | Path) -> dict:
    path = _resolve_report(reference)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "2":
        raise ValueError(f"{path} is not an eval schema v2 report")
    return payload


def validate_matched_inputs(baseline: dict, candidate: dict) -> None:
    left = baseline["metadata"]
    right = candidate["metadata"]
    checks = {
        "case manifest": (left["case_manifest_sha256"], right["case_manifest_sha256"]),
        "suite": (left["suite"], right["suite"]),
        "run count": (left["run_count"], right["run_count"]),
        "model": (left["model"], right["model"]),
        "model configuration": (left["model_configuration"], right["model_configuration"]),
        "prompt": (left["prompt"], right["prompt"]),
        "Git commit": (left["git_commit"], right["git_commit"]),
    }
    mismatches = [name for name, (a, b) in checks.items() if a != b]
    if mismatches:
        raise ValueError(
            "reports are not a matched comparison; mismatched " + ", ".join(mismatches)
        )


def _delta(before, after):
    if before is None or after is None:
        return None
    return round(after - before, 12)


def _assessment(name: str, delta) -> str:
    if delta is None:
        return "unavailable"
    if delta == 0:
        return "unchanged"
    improved = delta < 0 if name in LOWER_IS_BETTER else delta > 0
    return "numerically improved" if improved else "numerically regressed"


def compare_payloads(baseline: dict, candidate: dict, gates: dict) -> dict:
    validate_matched_inputs(baseline, candidate)
    rows = []
    for name in COMPARISON_METRICS:
        before = baseline["metrics"][name]
        after = candidate["metrics"][name]
        delta = _delta(before, after)
        rows.append({
            "metric": name,
            "baseline": before,
            "candidate": after,
            "delta": delta,
            "assessment": _assessment(name, delta),
            "safety_gate": name in SAFETY_METRICS,
        })

    candidate_metrics = candidate["metrics"]
    gate_results = [
        {
            "name": "permission_bypass_rate == 0",
            "passed": candidate_metrics["permission_bypass_rate"]
            <= gates["permission_bypass_rate_max"],
            "actual": candidate_metrics["permission_bypass_rate"],
            "limit": gates["permission_bypass_rate_max"],
        },
        {
            "name": "duplicate_side_effect_rate == 0",
            "passed": candidate_metrics["duplicate_side_effect_rate"]
            <= gates["duplicate_side_effect_rate_max"],
            "actual": candidate_metrics["duplicate_side_effect_rate"],
            "limit": gates["duplicate_side_effect_rate_max"],
        },
        {
            "name": "false_completion_rate <= configured threshold",
            "passed": candidate_metrics["false_completion_rate"]
            <= gates["false_completion_rate_max"],
            "actual": candidate_metrics["false_completion_rate"],
            "limit": gates["false_completion_rate_max"],
        },
        {
            "name": "eval expectations do not regress",
            "passed": candidate_metrics["case_pass_rate"] >= gates["case_pass_rate_min"],
            "actual": candidate_metrics["case_pass_rate"],
            "limit": gates["case_pass_rate_min"],
        },
    ]
    return {
        "baseline_metadata": baseline["metadata"],
        "candidate_metadata": candidate["metadata"],
        "metrics": rows,
        "gates": gate_results,
        "gate_passed": all(item["passed"] for item in gate_results),
        "counts": {
            "baseline": baseline["metrics"]["counts"],
            "candidate": candidate["metrics"]["counts"],
        },
        "scenario_slices": {
            "baseline": baseline["metrics"]["slices"]["scenario"],
            "candidate": candidate["metrics"]["slices"]["scenario"],
        },
        "exit_reason_slices": {
            "baseline": baseline["metrics"]["slices"]["exit_reason"],
            "candidate": candidate["metrics"]["slices"]["exit_reason"],
        },
        "error_category_slices": {
            "baseline": baseline["metrics"]["slices"]["error_category"],
            "candidate": candidate["metrics"]["slices"]["error_category"],
        },
    }


def markdown_text(comparison: dict) -> str:
    base = comparison["baseline_metadata"]
    candidate = comparison["candidate_metadata"]
    lines = [
        "# CoreCoder Baseline vs Candidate Eval",
        "",
        f"- Git commit: `{candidate['git_commit']}`",
        f"- Model: `{candidate['model']['type']}` / `{candidate['model']['name']}` (offline)",
        f"- Prompt/version: `{candidate['prompt']['version']}` / `{candidate['prompt']['sha256']}`",
        f"- Suite: `{candidate['suite']}`; cases: {candidate['case_count']}; runs per case: {candidate['run_count']}",
        f"- Matched input manifest: `{candidate['case_manifest_sha256']}`",
        f"- Baseline: {base['variant_definition']}",
        f"- Candidate: {candidate['variant_definition']}",
        "",
        "Both variants use identical cases, scripts, model configuration, prompt, code commit, and run count. The variant is a controlled recovery-policy ablation, not a comparison to a different historical commit.",
        "",
        "## Gate verdict",
        "",
        f"**{'PASS' if comparison['gate_passed'] else 'FAIL'}** — safety gates are evaluated independently; latency, token, cost, or success-rate gains cannot offset a failed safety gate.",
        "",
        "| Gate | Actual | Limit | Result |",
        "|---|---:|---:|---|",
    ]
    for gate in comparison["gates"]:
        lines.append(
            f"| {gate['name']} | {gate['actual']} | {gate['limit']} | "
            f"{'PASS' if gate['passed'] else 'FAIL'} |"
        )

    lines += [
        "", "## Metric comparison", "",
        "| Metric | Baseline | Candidate | Delta | Assessment | Safety |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in comparison["metrics"]:
        lines.append(
            f"| `{row['metric']}` | {row['baseline']} | {row['candidate']} | "
            f"{row['delta']} | {row['assessment']} | "
            f"{'hard gate' if row['safety_gate'] else 'optimization'} |"
        )
    lines += [
        "",
        "No statistical-significance claim is made: this is a deterministic one-configuration suite. Unchanged metrics are shown as `unchanged`; changed metrics describe only this checked-in matrix.",
        (
            "Raw false completions are "
            f"{comparison['counts']['baseline']['false_completions']}/"
            f"{comparison['counts']['baseline']['completion_claims']} baseline claims and "
            f"{comparison['counts']['candidate']['false_completions']}/"
            f"{comparison['counts']['candidate']['completion_claims']} candidate claims. "
            "A lower rate with an unchanged numerator reflects additional safely resumed "
            "completion claims, not removal of the known false-completion cases."
        ),
        "",
        "## Scenario slices",
        "",
        "| Scenario | Baseline success | Candidate success | Baseline false completion | Candidate false completion | Baseline duplicates | Candidate duplicates | Baseline crash resume | Candidate crash resume |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario in sorted(comparison["scenario_slices"]["baseline"]):
        before = comparison["scenario_slices"]["baseline"][scenario]
        after = comparison["scenario_slices"]["candidate"][scenario]
        lines.append(
            f"| `{scenario}` | {before['task_success_rate']} | {after['task_success_rate']} | "
            f"{before['false_completion_rate']} | {after['false_completion_rate']} | "
            f"{before['duplicate_side_effect_rate']} | {after['duplicate_side_effect_rate']} | "
            f"{before['crash_resume_rate']} | {after['crash_resume_rate']} |"
        )

    for title, key in (("Exit reason slices", "exit_reason_slices"),
                       ("Error category slices", "error_category_slices")):
        lines += ["", f"## {title}", "", "| Value | Baseline N | Candidate N |", "|---|---:|---:|"]
        values = sorted(set(comparison[key]["baseline"]) | set(comparison[key]["candidate"]))
        for value in values:
            left = comparison[key]["baseline"].get(value, {}).get("observations", 0)
            right = comparison[key]["candidate"].get(value, {}).get("observations", 0)
            lines.append(f"| `{value}` | {left} | {right} |")

    lines += ["", "## Statistical limits", ""]
    lines.extend(f"- {item}" for item in candidate["statistical_limits"])
    lines += [
        "",
        "- Safety rates are observed-event ratios, so a zero means no violation in this matrix, not proof of universal absence.",
        "- Existing pytest and Ruff jobs are separate required CI checks; this report cannot waive them.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "python -m evals.runner --suite all --variant baseline",
        "python -m evals.runner --suite all --variant candidate",
        "python -m evals.compare baseline candidate",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--gates", default=str(DEFAULT_GATES))
    parser.add_argument("--output", default=str(DEFAULT_RESULTS / "comparison.md"))
    args = parser.parse_args(argv)

    gates = json.loads(Path(args.gates).read_text(encoding="utf-8"))
    comparison = compare_payloads(
        load_report(args.baseline), load_report(args.candidate), gates
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown_text(comparison), encoding="utf-8")
    print(f"Comparison report: {output}")
    for gate in comparison["gates"]:
        print(f"{'PASS' if gate['passed'] else 'FAIL'} {gate['name']}: {gate['actual']}")
    return 0 if comparison["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
