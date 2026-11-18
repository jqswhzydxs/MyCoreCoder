"""SQLite checkpoints with JSON-only values and content-free failure writes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class JsonCheckpointSerializer:
    def dumps_typed(self, value):
        return "json", json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")

    def loads_typed(self, value):
        kind, body = value
        if kind != "json":
            raise ValueError("workflow checkpoints only accept JSON")
        return json.loads(body)


@contextmanager
def sqlite_checkpointer(path: Path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    class SafeSqliteSaver(SqliteSaver):
        def put_writes(self, config, writes, task_id, task_path=""):
            # LangGraph can persist repr(exception) in its error channel.
            # Provider/source exception bodies never belong in checkpoints.
            safe = [(channel, "workflow_stage_failed" if channel == "__error__" else value) for channel, value in writes]
            return super().put_writes(config, safe, task_id, task_path)

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        saver = SafeSqliteSaver(connection, serde=JsonCheckpointSerializer())
        # The pinned SDK uses a separate serializer for metadata. Keep that
        # channel JSON-only as well, including local status inspection.
        saver.jsonplus_serde = JsonCheckpointSerializer()
        yield saver
    finally:
        connection.close()
