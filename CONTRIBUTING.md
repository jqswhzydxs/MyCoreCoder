# Contributing to the CoreCoder Reliability Fork

Thank you for improving this fork. Keep the project useful as both an executable reliability reference and an honest derivative of upstream CoreCoder.

## Attribution and scope

- Preserve the MIT license, upstream authorship, and the relationship documented in both READMEs.
- Describe inherited CoreCoder behavior as inherited work. Describe only changes introduced by this fork as fork contributions.
- Do not report a benchmark improvement unless it is reproduced from checked-in cases with matched inputs and configuration.
- Prefer Python's standard library and existing project abstractions. A large framework needs a concrete requirement and an ADR.

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Default development and CI must stay offline. Do not read, log, or add API keys. Do not put live-provider tests into the default suite.

## Required checks

```bash
pytest tests --ignore=tests/test_live_smoke.py -q
ruff check corecoder tests examples
python -m evals.readme_benchmark --check
```

Changes to evaluation or recovery should also regenerate the matched evidence:

```bash
python -m evals.runner --suite all --variant baseline
python -m evals.runner --suite all --variant candidate
python -m evals.compare baseline candidate
python -m evals.readme_benchmark --write
```

Review the JSON and Markdown diff. Safety gates may not be waived by improvements in latency, tokens, cost, or success rate.

## Compatibility rules

- Keep `Agent.chat()` returning a string. Put new structured behavior on `Agent.run()`.
- Existing string-returning tools must continue to work through `LegacyToolAdapter`.
- A model response alone must never transition a run to `VERIFIED`.
- A budget or cancellation stop must not start another side-effect tool or leave an orphaned tool reply.
- Ambiguous non-idempotent effects must become `UNKNOWN`, not be replayed optimistically.
- Trace and RunStore data must remain redacted and non-fatal to the primary task where specified.

## Pull-request checklist

- Add focused offline tests for normal, denied, interrupted, and failure paths.
- Update the relevant design note; add an ADR for a durable architectural decision.
- Run the required test and Ruff commands.
- If reports changed, state whether the difference is deterministic, simulated, or measured live; never imply statistical significance from one scripted run.
- Update `CHANGELOG.md` and release notes when the user-visible contract changes.
