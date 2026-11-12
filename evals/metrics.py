"""Metric aggregation and stable slices for the offline comparison harness."""

from __future__ import annotations

import math
from collections import Counter, defaultdict


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _average(values: list[float | int]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _p95(values: list[float | int]) -> float:
    """Nearest-rank P95; deterministic and meaningful for small suites."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(float(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]), 6)


def _slice(items: list[dict]) -> dict:
    claims = sum(bool(item["final_outcome"]["claims_completion"]) for item in items)
    crashes = sum(bool(item["crash_injected"]) for item in items)
    permission_checks = sum(item["permission_checks"] for item in items)
    side_effects = sum(item["side_effect_executions"] for item in items)
    return {
        "observations": len(items),
        "task_success_rate": _rate(sum(item["task_success"] for item in items), len(items)),
        "verified_completion_rate": _rate(
            sum(item["verification"]["status"] == "passed" for item in items), len(items)
        ),
        "false_completion_rate": _rate(
            sum(item["final_outcome"]["false_completion"] for item in items), claims
        ),
        "duplicate_side_effect_rate": _rate(
            sum(item["duplicate_side_effects"] for item in items), side_effects
        ),
        "permission_bypass_rate": _rate(
            sum(item["permission_bypasses"] for item in items), permission_checks
        ),
        "crash_resume_rate": _rate(
            sum(item["crash_resumed"] for item in items), crashes
        ),
        "average_rounds": _average([item["rounds"] for item in items]),
        "p95_rounds": _p95([item["rounds"] for item in items]),
        "average_latency_ms": _average([item["latency_ms"] for item in items]),
        "p95_latency_ms": _p95([item["latency_ms"] for item in items]),
    }


def aggregate(case_results: list[dict]) -> dict:
    observations = len(case_results)
    tool_calls = [call for case in case_results for call in case["tool_calls"]]
    completion_claims = sum(
        bool(case["final_outcome"]["claims_completion"]) for case in case_results
    )
    successes = sum(case["task_success"] for case in case_results)
    side_effect_executions = sum(case["side_effect_executions"] for case in case_results)
    permission_checks = sum(case["permission_checks"] for case in case_results)
    injected_crashes = sum(case["crash_injected"] for case in case_results)
    total_cost = sum(case["estimated_cost_usd"] for case in case_results)

    by_category: dict[str, list[dict]] = defaultdict(list)
    by_exit_reason: dict[str, list[dict]] = defaultdict(list)
    by_error_category: dict[str, list[dict]] = defaultdict(list)
    for case in case_results:
        by_category[case["category"]].append(case)
        by_exit_reason[case["exit_reason"]].append(case)
        categories = case["error_categories"] or ["none"]
        for category in categories:
            by_error_category[category].append(case)

    metrics = {
        "task_success_rate": _rate(successes, observations),
        "verified_completion_rate": _rate(
            sum(case["verification"]["status"] == "passed" for case in case_results),
            observations,
        ),
        "false_completion_rate": _rate(
            sum(case["final_outcome"]["false_completion"] for case in case_results),
            completion_claims,
        ),
        "duplicate_side_effect_rate": _rate(
            sum(case["duplicate_side_effects"] for case in case_results),
            side_effect_executions,
        ),
        "permission_bypass_rate": _rate(
            sum(case["permission_bypasses"] for case in case_results),
            permission_checks,
        ),
        "crash_resume_rate": _rate(
            sum(case["crash_resumed"] for case in case_results), injected_crashes
        ),
        "average_rounds": _average([case["rounds"] for case in case_results]),
        "p95_rounds": _p95([case["rounds"] for case in case_results]),
        "average_latency_ms": _average([case["latency_ms"] for case in case_results]),
        "p95_latency_ms": _p95([case["latency_ms"] for case in case_results]),
        "prompt_tokens": sum(case["prompt_tokens"] for case in case_results),
        "completion_tokens": sum(case["completion_tokens"] for case in case_results),
        "average_prompt_tokens": _average(
            [case["prompt_tokens"] for case in case_results]
        ),
        "average_completion_tokens": _average(
            [case["completion_tokens"] for case in case_results]
        ),
        "estimated_cost_per_success": round(total_cost / successes, 12) if successes else None,
        # Harness-conformance diagnostics retained from v1.
        "case_pass_rate": _rate(sum(case["passed"] for case in case_results), observations),
        "tool_selection_accuracy": _rate(
            sum(case["tool_selection_correct"] for case in case_results), observations
        ),
        "argument_valid_rate": _rate(
            sum(call["argument_valid"] for call in tool_calls), len(tool_calls)
        ),
        "forbidden_action_rate": _rate(
            sum(call["forbidden"] for call in tool_calls), len(tool_calls)
        ),
        "average_tool_calls": _average([len(case["tool_calls"]) for case in case_results]),
        "counts": {
            "observations": observations,
            "unique_cases": len({case["id"] for case in case_results}),
            "passed_cases": sum(case["passed"] for case in case_results),
            "successful_tasks": successes,
            "completion_claims": completion_claims,
            "verified_completions": sum(
                case["verification"]["status"] == "passed" for case in case_results
            ),
            "false_completions": sum(
                case["final_outcome"]["false_completion"] for case in case_results
            ),
            "tool_calls": len(tool_calls),
            "side_effect_executions": side_effect_executions,
            "duplicate_side_effects": sum(
                case["duplicate_side_effects"] for case in case_results
            ),
            "permission_checks": permission_checks,
            "permission_bypasses": sum(
                case["permission_bypasses"] for case in case_results
            ),
            "injected_crashes": injected_crashes,
            "resumed_crashes": sum(case["crash_resumed"] for case in case_results),
        },
        "slices": {
            "scenario": {
                name: _slice(items) for name, items in sorted(by_category.items())
            },
            "exit_reason": {
                name: _slice(items) for name, items in sorted(by_exit_reason.items())
            },
            "error_category": {
                name: _slice(items) for name, items in sorted(by_error_category.items())
            },
        },
        "distributions": {
            "exit_reason": dict(sorted(Counter(
                case["exit_reason"] for case in case_results
            ).items())),
            "error_category": dict(sorted(Counter(
                category
                for case in case_results
                for category in (case["error_categories"] or ["none"])
            ).items())),
        },
    }
    return metrics
