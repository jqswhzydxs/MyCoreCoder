"""No keys/network: real SQLite preference lifecycle across new service instances."""

import json
import tempfile
from pathlib import Path

from corecoder.knowledge.scope import create_scope
from corecoder.memory.service import MemoryService


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="corecoder-memory-demo-") as directory:
        scope = create_scope(Path(directory))
        first = MemoryService(scope)
        chinese = first.remember("回答优先使用中文", key="response.language", operation_id="first-user-turn")
        second = MemoryService(scope)
        assert second.recall("Explain RunStore").memories[0].memory_id == chinese.memory_id
        english = second.remember("Answer in English", key="response.language", operation_id="second-user-turn")
        assert english.supersedes == chinese.memory_id and second.show(chinese.memory_id).status == "superseded"
        assert second.recall("Explain RunStore").memories == (english,)
        second.forget(english.memory_id)
        third = MemoryService(scope)
        assert third.recall("Explain RunStore").memories == ()
        replay = third.remember("Answer in English", key="response.language", operation_id="second-user-turn")
        assert replay.status == "deleted" and replay.content == "" and third.recall("Explain RunStore").memories == ()
        print(json.dumps({"mode": "offline_no_model", "cross_session_recall": True, "superseded": True,
                          "deleted_body": replay.content, "old_operation_replay": replay.status,
                          "remaining_active": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
