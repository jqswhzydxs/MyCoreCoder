# 05 · Deterministic baseline/candidate evaluation

## Purpose and attribution

The Eval Harness measures reliability changes in this fork. It does not claim that inherited upstream capabilities are personal work, and its `baseline` variant is not untouched upstream CoreCoder. Baseline and candidate execute the same fork code; the controlled variable is automatic durable resume.

- baseline: durable journal present, automatic resume disabled;
- candidate: current safe RunStore resume policy enabled.

Both reports must match on case manifest, suite, run count, model and configuration, Prompt/version, and Git commit. `evals.compare` refuses mismatched reports.

## Suite

Forty JSON cases cover normal, boundary, permission, malformed tool calls, transient failures, timeouts, crash/resume, duplicate side effects, false completion, and no-progress loops. `FaultInjectingLLM` and tool wrappers deterministically inject 429/5xx/network errors, malformed JSON, timeout, lost receipts, and event-boundary crashes.

Each case declares input, scripted responses, expected/forbidden calls, expected file state, and expected outcome. A failure names the case and its individual reasons.

## Metrics and gates

Reports include task and verified success, false completion, duplicate side effects, permission bypass, crash resume, average/P95 rounds and simulated latency, tokens, synthetic estimated cost, and slices by scenario, exit reason, and error category.

Hard candidate gates are evaluated independently:

- permission bypass rate equals zero;
- duplicate side-effect rate equals zero;
- false-completion rate is below the configured ceiling;
- case expectations do not regress.

Latency, tokens, cost, or task success cannot compensate for a safety-gate failure.

## Reproduce

```bash
python -m evals.runner --suite all --variant baseline
python -m evals.runner --suite all --variant candidate
python -m evals.compare baseline candidate
python -m evals.readme_benchmark --check
```

The checked-in comparison records 40 cases and one run per case. Candidate observed 90% task success, 5% verified completion, 9.0909% false completion, zero duplicate side effects, zero permission bypass, and 100% crash resume. Baseline observed 80%, 5%, 11.1111%, 28.5714%, zero, and zero respectively. These values are generated into both READMEs from the JSON reports.

## Statistical limits

The scripted suite is deterministic and estimates neither live-model variance nor production performance. Latency is simulated work-unit latency, token use is a harness count, and cost uses fixed test rates rather than provider billing. P95 is nearest-rank over the recorded case runs. Zero observed violations do not prove universal absence. Live-provider smoke tests remain excluded from default CI.
