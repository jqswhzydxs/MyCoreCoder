# ADR-0003: Require external evidence for verified completion

- Status: Accepted
- Date: 2026-11-04

## Context

Language models can produce plausible completion statements after a failed, denied, timed-out, or never-executed tool call. Treating final prose as task state makes false completion indistinguishable from verified success and provides no evidence trail.

## Decision

The model is not the final judge. Plain model text moves a run to `VERIFYING`. Only caller-configured Verifiers may produce the evidence needed for `VERIFIED`. Missing evidence yields `UNVERIFIED`; a failed hard condition yields `NEEDS_ACTION`; verification runtime errors fail explicitly. `AgentResult` stores model text separately from verification state and evidence.

Verification commands must come from trusted configuration and pass through permissions. The runtime never executes a command synthesized from the model's final answer.

## Consequences

- Every verified result is traceable to external evidence.
- False completion becomes measurable in Eval instead of being hidden in prose.
- `Agent.chat()` remains usable but visibly marks results that are not verified.
- Callers must define meaningful success criteria and Verifiers; without them, honest results remain unverified.
- Tests and file checks prove only their configured properties, so verifier quality remains part of application design.
