# Reliability Preview — delivery candidate

Status: **unreleased working-tree preview**. This is not an upstream CoreCoder release and is not currently represented by a new fork commit.

## What this candidate demonstrates

Starting from upstream CoreCoder 0.8.0 at `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0`, the fork adds explicit run state, governed tool contracts and scheduling, budgets/cancellation, redacted Trace, evidence-based completion, SQLite crash recovery, and a deterministic baseline/candidate Eval Harness. The package and CLI remain named `corecoder` for compatibility.

Run the offline acceptance demo:

```bash
python -m examples.reliablecoder_demo
```

It makes a real file edit, crashes after execution but before receipt persistence, resumes without repeating the edit, runs real pytest, validates Trace, and ends `VERIFIED` only when external evidence passes.

## Checked-in evaluation evidence

The matched 40-case, one-run-per-case scripted comparison records:

| Metric | Baseline | Candidate |
|---|---:|---:|
| Task success | 80% | 90% |
| Verified completion | 5% | 5% |
| False completion | 11.1111% | 9.0909% |
| Duplicate side effects | 28.5714% | 0% |
| Permission bypass | 0% | 0% |
| Crash resume | 0% | 100% |
| P95 rounds | 3 | 2 |

`baseline` is the same fork with automatic durable resume disabled; it is not untouched upstream. Raw false completions remain 2 in both variants. Detailed evidence and limits are in `evals/results/comparison.md`.

## Before publishing a release

- Commit the fork work so reports can identify a fork commit rather than only the upstream base.
- Decide package/distribution naming without overwriting upstream authorship.
- Re-run the supported OS/Python CI matrix.
- Consider a dependency lock or hashed constraints for long-term environment reproduction.
- Keep live-provider benchmark results separate from deterministic offline evidence and record their model, parameters, price source, run count, and variance.

## Known limits

The checked-in Eval is deterministic, not a live-model benchmark. Latency is simulated, cost uses synthetic rates, and one run per case does not establish statistical significance. External non-idempotent effects can remain `UNKNOWN`; Verifier configuration must be re-supplied on resume; permission-aware shell execution is not a security sandbox.
