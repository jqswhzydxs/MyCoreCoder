# Memory lifecycle comparison

Deterministic lifecycle and prompt-context selection only; live model constraint adherence is not evaluated.

| Memory | Context checks | Necessary recall | Precision | Deleted residual | Scope returns |
|---|---:|---:|---:|---:|---:|
| off | 14/21 | 0.0 | None | 0 | 0 |
| on | 21/21 | 1.0 | 1.0 | 0 | 0 |
