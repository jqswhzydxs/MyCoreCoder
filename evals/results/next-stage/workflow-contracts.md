# Graph recovery contracts

Fixed fault/control contracts with actual StateGraph, SQLite and tool effects. No live model quality or matched runtime/graph performance improvement is claimed.

Passed: 12/12; pytest exit: 0

| Scenario | Passed |
|---|---|
| mapping-before-child | True |
| terminal-before-checkpoint | True |
| write-without-receipt | True |
| paused-recovery | True |
| unknown-external-effect | True |
| lost-model-usage | True |
| concurrent-recovery | True |
| missing-verifier | True |
| memory-write-replay | True |
| shared-budget | True |
| cancel-before-plan | True |
| input-project-config-payload | True |
