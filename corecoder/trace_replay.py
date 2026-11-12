"""Offline replay and latency summary for CoreCoder JSONL traces."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

_FINISHED_STEPS = {
    "model_finished",
    "model_failed",
    "tool_finished",
    "tool_failed",
}


def load_trace(path: str | Path) -> list[dict]:
    events = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
        events.append(event)
    return events


def summarize(events: list[dict], slowest_limit: int = 5) -> list[dict]:
    grouped = defaultdict(list)
    for event in events:
        grouped[event.get("run_id", "unknown")].append(event)

    summaries = []
    for run_id, run_events in grouped.items():
        ordered = sorted(run_events, key=lambda event: event.get("step_id", 0))
        run_finished = next(
            (event for event in reversed(ordered) if event.get("event_type") == "run_finished"),
            None,
        )
        timed_steps = [
            event
            for event in ordered
            if event.get("event_type") in _FINISHED_STEPS and event.get("duration_ms") is not None
        ]
        slowest = sorted(
            timed_steps,
            key=lambda event: (-event["duration_ms"], event.get("step_id", 0)),
        )[:slowest_limit]
        summaries.append({
            "trace_id": ordered[0].get("trace_id") if ordered else None,
            "run_id": run_id,
            "exit_reason": run_finished.get("outcome") if run_finished else "unfinished",
            "total_duration_ms": run_finished.get("duration_ms") if run_finished else None,
            "model_duration_ms": round(sum(
                event.get("duration_ms") or 0
                for event in timed_steps
                if event.get("event_type", "").startswith("model_")
            ), 3),
            "tool_duration_ms": round(sum(
                event.get("duration_ms") or 0
                for event in timed_steps
                if event.get("event_type", "").startswith("tool_")
            ), 3),
            "slowest_steps": slowest,
            "key_path": [
                {
                    "step_id": event.get("step_id"),
                    "event_type": event.get("event_type"),
                    "round": event.get("round"),
                    "tool_name": event.get("tool_name"),
                    "tool_call_id": event.get("tool_call_id"),
                    "outcome": event.get("outcome"),
                    "duration_ms": event.get("duration_ms"),
                }
                for event in ordered
            ],
        })
    return sorted(summaries, key=lambda summary: summary["run_id"])


def render(summaries: list[dict]) -> str:
    lines = []
    for summary in summaries:
        lines.extend([
            f"Run {summary['run_id']} (trace {summary['trace_id']})",
            f"Exit: {summary['exit_reason']}",
            (
                f"Latency: total={summary['total_duration_ms']} ms, "
                f"model={summary['model_duration_ms']} ms, "
                f"tools={summary['tool_duration_ms']} ms"
            ),
            "Key path:",
        ])
        for event in summary["key_path"]:
            subject = event["tool_name"] or ""
            duration = "" if event["duration_ms"] is None else f" {event['duration_ms']} ms"
            outcome = "" if event["outcome"] is None else f" [{event['outcome']}]"
            lines.append(
                f"  {event['step_id']:03d} {event['event_type']} {subject}{outcome}{duration}".rstrip()
            )
        lines.append("Slowest steps:")
        if not summary["slowest_steps"]:
            lines.append("  (none)")
        for event in summary["slowest_steps"]:
            subject = event.get("tool_name") or event.get("model") or ""
            lines.append(
                f"  {event['duration_ms']} ms  {event['event_type']} {subject}".rstrip()
            )
        lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_file", help="JSONL trace produced by --trace-file")
    parser.add_argument("--slowest", type=int, default=5, help="Number of slowest model/tool steps to show")
    args = parser.parse_args(argv)
    print(render(summarize(load_trace(args.trace_file), slowest_limit=max(0, args.slowest))), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
