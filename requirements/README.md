# Environment snapshots

These files record installed dependencies for specific tested Windows/Python combinations. They are not cross-platform locks, and they do not pin the local editable `corecoder` source. Use the accompanying source manifests and check reports to identify the implementation.

- `base-py310-windows.lock.txt` and `knowledge-py314-windows.lock.txt`: historical initial Prompt integration environments from 2026-11-13. The latter does not include the later Chroma pipeline.
- `next-stage-knowledge-py311-windows.lock.txt` and `next-stage-knowledge-py312-windows.lock.txt`: complete repository knowledge environments from 2026-11-17, including actual Chroma/FTS5 write/read/reopen/delete and memory tests.
- `next-stage-graph-py311-windows.lock.txt`, `next-stage-graph-py312-windows.lock.txt`, `next-stage-graph-py314-windows.lock.txt`: tested graph + knowledge environments (113 dependency packages each), with actual StateGraph/SQLite and recovery contracts; these snapshots do not include the later MCP SDK.
- `next-stage-mcp-py311-windows.lock.txt`, `next-stage-mcp-py312-windows.lock.txt`, `next-stage-mcp-py314-windows.lock.txt`: knowledge + graph + official MCP SDK environments. Python 3.11 includes the editable import-hook dependency (124 non-project packages); 3.12/3.14 have 123. Exact Python/SQLite versions and dependency checks are recorded in `evals/results/next-stage/mcp-environments.json`.

To reproduce a snapshot in a matching isolated environment, install it with `python -m pip install -r FILE`, then install this project with `python -m pip install --no-deps -e .`. Use `corecoder[knowledge]` when resolving dependencies on another platform; run the mandatory extension tests before claiming support. No model weights or user indexes are included.

Current editable builds use Hatchling's exact import hook to support non-ASCII Windows checkout paths under Python 3.10/3.11. The editable package metadata requires `editables~=0.3`; older snapshots without it need that declared dependency when installing the current editable source. Standard wheels require no editable hook. Always run `pip check` after using a snapshot with a different source revision.
