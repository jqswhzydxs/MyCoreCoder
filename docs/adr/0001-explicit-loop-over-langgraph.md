# ADR-0001: Evolve the explicit loop instead of rewriting with LangGraph

- Status: Accepted
- Date: 2026-11-04

## Context

The upstream project's educational value and compatibility depend on a small, readable Python loop and `Agent.chat()`. Reliability work needs explicit state transitions, scheduling, verification, budgets, Trace, and persistence. A graph framework could model these concepts, but adopting one would also replace the inherited execution path and add a large runtime dependency.

## Decision

Keep the existing loop and extract its implicit responsibilities into standard-library-oriented components: `RunState`, `ToolScheduler`, Verifiers, `RunBudgetController`, Trace sinks, and `RunStore`. Add `Agent.run()` for structured behavior and keep `Agent.chat()` as the compatibility facade.

This is a scope and migration decision, not a claim that LangGraph is generally unsuitable. A future adapter may expose this runtime as graph nodes if a concrete integration requires it.

## Consequences

- Existing tools, prompt behavior, and tests remain useful during incremental migration.
- State and recovery semantics stay visible in this repository rather than being delegated to a framework runtime.
- Installation remains lightweight and the persistence core uses the Python standard library.
- The fork owns more orchestration code and must test transition, interruption, and recovery edge cases itself.
- It does not automatically gain ecosystem features such as distributed graph execution or framework visualization.
