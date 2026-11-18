# Memory and workflow contracts for subsequent stages

This note preserves the original S3/S4 contracts. Implemented behavior and limits are now recorded in [project memory](08-project-memory.md) and [durable graph workflows](09-durable-graph-workflows.md). The existing explicit Agent loop, RunStore, ToolScheduler, Permission and Verifier remain authoritative; wording below describes the original design intent rather than additional acceptance evidence.

## MemoryRecord

The memory service will bind `memory_id`, `owner_id`, `project_id`, kind, key, content, trusted source reference/hash, evidence references, status, UTC creation/update/expiry timestamps, supersedes, write operation identity and schema version. Status is candidate/active/superseded/expired/deleted; an invalid source is excluded as stale. Caller context supplies scope, time and trusted provenance. Model extraction produces proposals only.

Explicit user preferences and reusable verified task experience have separate admission rules. Experience requires a current VERIFIED RunStore record and complete evidence, not a model claim. Deletion removes content from recall/cache and leaves a content-free operation tombstone so old writes cannot revive forgotten memories. Current user instructions outrank historical constraints, which never grant tool permissions.

## WorkflowState and operation identity

Graph checkpoints will contain only schema, owner/project/thread/turn, request reference, snapshot, retrieval/memory/plan/result references, attempt, child run ID, budget usage and phase/status. Raw requests and evidence belong in a purpose-bound payload store with expiry and content hashes. Losing payloads or a trusted verification policy requires action instead of silently replacing inputs.

Operations will bind `(project, thread, turn, plan hash, attempt)` to a stable public Agent run identity before any tool side effect. The plan hash includes trusted verification policy/tool configuration and input version. SQLite claims provide cross-process exclusion. Recovery queries RunStore first: reuse terminal results, resume the same nonterminal run, preserve PAUSED, and refuse ambiguous UNKNOWN effects. Graph completion never upgrades a coding result to VERIFIED.

`steps[].expected_evidence` in the existing TaskPlan is an existing evidence-ID list, and nested IDs are now included in membership validation. Future evidence needs must be described in actions/unknowns until a distinct requirement type is introduced.

The shared budget must span planner/answer/memory/summary/query and document embeddings plus the child Agent. S4 will add a compatible Agent LLM bridge and durable reservation/settlement so recovery cannot reset consumption. SDK retries stay disabled under the gateway path. No global exactly-once guarantee is claimed across independent stores and external services.
