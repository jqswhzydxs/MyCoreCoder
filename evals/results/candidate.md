# CoreCoder Offline Agent Eval: candidate

- Git commit: `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0`
- Variant: `candidate` — current runtime with safe RunStore resume enabled
- Suite/runs: `all` / 1
- Cases/observations: 40 / 40
- Case manifest: `cbf49d611eaabf64d0b2df3bd086a3332482a80c7cb6362419ee35b9a2f3ad54`
- Harness: `2.0` / case schema `2`
- Model: `FaultInjectingLLM` (`scripted-eval-v2`, offline)
- Prompt/version: `corecoder-0.8.0` (`af04ba34ec35113a58a21c428789bd55daad2623cdb9bd2494c3fc0478ecbee8`)
- Environment: CPython 3.14.8 on `Windows-11-10.0.26100-SP0`

## Metrics

| Metric | Value | Definition |
|---|---:|---|
| `task_success_rate` | 0.9 | Cases satisfying their externally declared task outcome / observations |
| `verified_completion_rate` | 0.05 | Verifier-passed runs / observations |
| `false_completion_rate` | 0.090909 | Completion claims missing required external evidence / completion claims |
| `duplicate_side_effect_rate` | 0.0 | Repeated identical mutating executions / mutating executions |
| `permission_bypass_rate` | 0.0 | Blocked scenarios that executed a mutation / permission checks |
| `crash_resume_rate` | 1.0 | Injected crashes safely resumed / injected crashes |
| `average_rounds` | 1.95 | Mean model rounds per observation |
| `p95_rounds` | 2.0 | Nearest-rank P95 model rounds |
| `average_latency_ms` | 148.825 | Mean deterministic simulated latency |
| `p95_latency_ms` | 1020.0 | Nearest-rank P95 deterministic simulated latency |
| `prompt_tokens` | 936 | Total scripted prompt tokens |
| `completion_tokens` | 188 | Total scripted completion tokens |
| `estimated_cost_per_success` | 3.6444444e-05 | Synthetic fixed-rate cost / successful tasks |

## Scenario slices

| Scenario | N | Success | Verified | False completion | Duplicate side effect | Permission bypass | Crash resume | Avg/P95 rounds | Avg/P95 latency ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `boundary` | 4 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 2.0 / 2.0 | 26.25 / 30.0 |
| `crash_resume` | 4 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 2.0 / 2.0 | 25.0 / 25.0 |
| `duplicate_side_effect` | 4 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 2.0 / 2.0 | 25.0 / 25.0 |
| `false_completion` | 4 | 0.0 | 0.0 | 0.5 | 0.0 | 0.0 | 0.0 | 1.25 / 2.0 | 13.75 / 25.0 |
| `malformed_tool_call` | 4 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 2.0 / 2.0 | 20.0 / 20.0 |
| `no_progress_loop` | 4 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 2.0 / 2.0 | 27.5 / 30.0 |
| `normal` | 4 | 1.0 | 0.5 | 0.0 | 0.0 | 0.0 | 0.0 | 2.0 / 2.0 | 26.25 / 30.0 |
| `permission` | 4 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 2.0 / 2.0 | 20.0 / 20.0 |
| `timeout` | 4 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 2.25 / 3.0 | 1273.75 / 2020.0 |
| `transient_failure` | 4 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 2.0 / 2.0 | 30.75 / 39.0 |

## Exit reasons

| Value | Observations | Success rate |
|---|---:|---:|
| `insufficient_evidence` | 1 | 0.0 |
| `max_rounds` | 1 | 1.0 |
| `model_stopped_unverified` | 30 | 1.0 |
| `no_progress` | 3 | 1.0 |
| `success_criteria_met` | 2 | 1.0 |
| `verification_failed` | 3 | 0.0 |

## Error categories

| Value | Observations | Success rate |
|---|---:|---:|
| `invalid_arguments` | 2 | 1.0 |
| `none` | 23 | 0.826087 |
| `permission_denied` | 4 | 1.0 |
| `timeout` | 4 | 1.0 |
| `tool_error` | 3 | 1.0 |
| `tool_exception` | 2 | 1.0 |
| `unknown_tool` | 2 | 1.0 |

## Cases

| Case | Scenario | Result | Exit | Verification | Rounds | Latency ms | Duplicates |
|---|---|---|---|---|---:|---:|---:|
| `read_file_success` | `normal` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `missing_file` | `boundary` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `write_file_success` | `normal` | PASS | `success_criteria_met` | `passed` | 2 | 25.0 | 0 |
| `edit_file_success` | `normal` | PASS | `success_criteria_met` | `passed` | 2 | 25.0 | 0 |
| `permission_denied` | `permission` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `noninteractive_write_denied` | `permission` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `unknown_tool` | `malformed_tool_call` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `missing_required_argument` | `malformed_tool_call` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `tool_exception` | `transient_failure` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `parallel_reads` | `normal` | PASS | `model_stopped_unverified` | `not_run` | 2 | 30.0 | 0 |
| `parallel_write_conflict` | `boundary` | PASS | `model_stopped_unverified` | `not_run` | 2 | 30.0 | 0 |
| `max_rounds` | `no_progress_loop` | PASS | `max_rounds` | `not_run` | 2 | 30.0 | 0 |
| `false_completion_missing_evidence` | `false_completion` | PASS | `verification_failed` | `failed` | 2 | 25.0 | 0 |
| `verification_test_failure` | `false_completion` | PASS | `verification_failed` | `failed` | 1 | 10.0 | 0 |
| `verification_evidence_missing` | `false_completion` | PASS | `insufficient_evidence` | `insufficient_evidence` | 1 | 10.0 | 0 |
| `verification_partial_success` | `false_completion` | PASS | `verification_failed` | `failed` | 1 | 10.0 | 0 |
| `boundary_empty_file` | `boundary` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `boundary_directory_read` | `boundary` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `permission_edit_denied` | `permission` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `permission_overwrite_noninteractive` | `permission` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `malformed_invalid_json_arguments` | `malformed_tool_call` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `malformed_unexpected_argument` | `malformed_tool_call` | PASS | `model_stopped_unverified` | `not_run` | 2 | 20.0 | 0 |
| `transient_llm_429` | `transient_failure` | PASS | `model_stopped_unverified` | `not_run` | 2 | 32.0 | 0 |
| `transient_llm_5xx` | `transient_failure` | PASS | `model_stopped_unverified` | `not_run` | 2 | 39.0 | 0 |
| `transient_llm_connection` | `transient_failure` | PASS | `model_stopped_unverified` | `not_run` | 2 | 32.0 | 0 |
| `timeout_read` | `timeout` | PASS | `model_stopped_unverified` | `not_run` | 2 | 1020.0 | 0 |
| `timeout_write` | `timeout` | PASS | `model_stopped_unverified` | `not_run` | 2 | 1520.0 | 0 |
| `timeout_edit` | `timeout` | PASS | `model_stopped_unverified` | `not_run` | 2 | 2020.0 | 0 |
| `timeout_retry_success` | `timeout` | PASS | `model_stopped_unverified` | `not_run` | 3 | 535.0 | 0 |
| `crash_after_tool_planned` | `crash_resume` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `crash_before_tool_execute` | `crash_resume` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `crash_after_tool_execute` | `crash_resume` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `crash_after_result_persisted` | `crash_resume` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `duplicate_response_lost_create` | `duplicate_side_effect` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `duplicate_response_lost_overwrite` | `duplicate_side_effect` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `duplicate_response_lost_nested` | `duplicate_side_effect` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `duplicate_response_lost_unicode` | `duplicate_side_effect` | PASS | `model_stopped_unverified` | `not_run` | 2 | 25.0 | 0 |
| `no_progress_repeated_read` | `no_progress_loop` | PASS | `no_progress` | `not_run` | 2 | 30.0 | 0 |
| `no_progress_repeated_missing` | `no_progress_loop` | PASS | `no_progress` | `not_run` | 2 | 30.0 | 0 |
| `no_progress_repeated_unknown` | `no_progress_loop` | PASS | `no_progress` | `not_run` | 2 | 20.0 | 0 |

## Failure details

All observations matched their declared expectations.

## Statistical limits

- Scripted cases are deterministic and do not estimate live-model variance.
- Latency is simulated work-unit latency, not wall-clock performance.
- Cost uses fixed harness rates ($1/M prompt, $2/M completion), not provider billing.
- P95 is the nearest-rank percentile over the recorded case runs.

## Reproduce

```bash
python -m evals.runner --suite all --variant candidate
```

The suite uses scripted responses, deterministic fault injection, and temporary directories only. It never calls a real provider.
