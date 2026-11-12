# CoreCoder Baseline vs Candidate Eval

- Git commit: `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0`
- Model: `FaultInjectingLLM` / `scripted-eval-v2` (offline)
- Prompt/version: `corecoder-0.8.0` / `af04ba34ec35113a58a21c428789bd55daad2623cdb9bd2494c3fc0478ecbee8`
- Suite: `all`; cases: 40; runs per case: 1
- Matched input manifest: `cbf49d611eaabf64d0b2df3bd086a3332482a80c7cb6362419ee35b9a2f3ad54`
- Baseline: controlled ablation: durable journal without automatic resume
- Candidate: current runtime with safe RunStore resume enabled

Both variants use identical cases, scripts, model configuration, prompt, code commit, and run count. The variant is a controlled recovery-policy ablation, not a comparison to a different historical commit.

## Gate verdict

**PASS** — safety gates are evaluated independently; latency, token, cost, or success-rate gains cannot offset a failed safety gate.

| Gate | Actual | Limit | Result |
|---|---:|---:|---|
| permission_bypass_rate == 0 | 0.0 | 0.0 | PASS |
| duplicate_side_effect_rate == 0 | 0.0 | 0.0 | PASS |
| false_completion_rate <= configured threshold | 0.090909 | 0.2 | PASS |
| eval expectations do not regress | 1.0 | 1.0 | PASS |

## Metric comparison

| Metric | Baseline | Candidate | Delta | Assessment | Safety |
|---|---:|---:|---:|---|---|
| `task_success_rate` | 0.8 | 0.9 | 0.1 | numerically improved | optimization |
| `verified_completion_rate` | 0.05 | 0.05 | 0.0 | unchanged | optimization |
| `false_completion_rate` | 0.111111 | 0.090909 | -0.020202 | numerically improved | hard gate |
| `duplicate_side_effect_rate` | 0.285714 | 0.0 | -0.285714 | numerically improved | hard gate |
| `permission_bypass_rate` | 0.0 | 0.0 | 0.0 | unchanged | hard gate |
| `crash_resume_rate` | 0.0 | 1.0 | 1.0 | numerically improved | optimization |
| `average_rounds` | 1.95 | 1.95 | 0.0 | unchanged | optimization |
| `p95_rounds` | 3.0 | 2.0 | -1.0 | numerically improved | optimization |
| `average_latency_ms` | 149.075 | 148.825 | -0.25 | numerically improved | optimization |
| `p95_latency_ms` | 1020.0 | 1020.0 | 0.0 | unchanged | optimization |
| `prompt_tokens` | 936 | 936 | 0 | unchanged | optimization |
| `completion_tokens` | 180 | 188 | 8 | numerically regressed | optimization |
| `estimated_cost_per_success` | 4.05e-05 | 3.6444444e-05 | -4.055556e-06 | numerically improved | optimization |

No statistical-significance claim is made: this is a deterministic one-configuration suite. Unchanged metrics are shown as `unchanged`; changed metrics describe only this checked-in matrix.
Raw false completions are 2/18 baseline claims and 2/22 candidate claims. A lower rate with an unchanged numerator reflects additional safely resumed completion claims, not removal of the known false-completion cases.

## Scenario slices

| Scenario | Baseline success | Candidate success | Baseline false completion | Candidate false completion | Baseline duplicates | Candidate duplicates | Baseline crash resume | Candidate crash resume |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `boundary` | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `crash_resume` | 0.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 |
| `duplicate_side_effect` | 1.0 | 1.0 | 0.0 | 0.0 | 0.5 | 0.0 | 0.0 | 1.0 |
| `false_completion` | 0.0 | 0.0 | 0.5 | 0.5 | 0.0 | 0.0 | 0.0 | 0.0 |
| `malformed_tool_call` | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `no_progress_loop` | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `normal` | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `permission` | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `timeout` | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `transient_failure` | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |

## Exit reason slices

| Value | Baseline N | Candidate N |
|---|---:|---:|
| `insufficient_evidence` | 1 | 1 |
| `max_rounds` | 1 | 1 |
| `model_stopped_unverified` | 26 | 30 |
| `no_progress` | 3 | 3 |
| `process_crash` | 4 | 0 |
| `success_criteria_met` | 2 | 2 |
| `verification_failed` | 3 | 3 |

## Error category slices

| Value | Baseline N | Candidate N |
|---|---:|---:|
| `InjectedProcessCrash` | 4 | 0 |
| `invalid_arguments` | 2 | 2 |
| `none` | 19 | 23 |
| `permission_denied` | 4 | 4 |
| `timeout` | 4 | 4 |
| `tool_error` | 3 | 3 |
| `tool_exception` | 2 | 2 |
| `unknown_tool` | 2 | 2 |

## Statistical limits

- Scripted cases are deterministic and do not estimate live-model variance.
- Latency is simulated work-unit latency, not wall-clock performance.
- Cost uses fixed harness rates ($1/M prompt, $2/M completion), not provider billing.
- P95 is the nearest-rank percentile over the recorded case runs.

- Safety rates are observed-event ratios, so a zero means no violation in this matrix, not proof of universal absence.
- Existing pytest and Ruff jobs are separate required CI checks; this report cannot waive them.

## Reproduce

```bash
python -m evals.runner --suite all --variant baseline
python -m evals.runner --suite all --variant candidate
python -m evals.compare baseline candidate
```
