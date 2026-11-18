"""Memory lifecycle, source authority and cross-session behavior with real SQLite."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from corecoder.knowledge.models import digest
from corecoder.knowledge.scope import create_scope
from corecoder.memory.cli import main
from corecoder.memory.models import MemoryError, SourceAuthority
from corecoder.memory.service import MemoryService
from corecoder.memory.store import MemoryStore


class Clock:
    def __init__(self):
        self.time = datetime(2026, 11, 17, tzinfo=timezone.utc)

    def __call__(self):
        return self.time

    def advance(self, seconds):
        self.time += timedelta(seconds=seconds)


def make_service(tmp_path, *, clock=None, store=None, run_store=None):
    return MemoryService(create_scope(tmp_path), store, clock=clock or Clock(), run_store=run_store)


def test_reopen_new_session_and_current_instruction_priority(tmp_path):
    first = make_service(tmp_path)
    memory = first.remember("优先使用中文回答", key="response.language", operation_id="turn-1")
    second = make_service(tmp_path)
    assert second.recall("Explain RunStore").memories == (memory,)
    override = second.recall("Answer in English", current_keys=("response.language",))
    assert override.memories == ()
    assert override.excluded[0]["reason"] == "current_instruction_overrides_history"
    constraints = second.plan_constraints("Plan", ["Do not edit files"])
    assert constraints[0] == "Do not edit files" and constraints[1]["authority"] == "historical_reference_only"


def test_local_owner_survives_minimal_stdio_environment(tmp_path, monkeypatch):
    original = create_scope(tmp_path)
    monkeypatch.delenv("COMPUTERNAME", raising=False)
    assert create_scope(tmp_path).identity() == original.identity()


def test_preference_supersession_and_operation_replay(tmp_path):
    service = make_service(tmp_path)
    old = service.remember("Use Chinese", operation_id="first")
    new = service.remember("Use English", operation_id="second")
    assert new.supersedes == old.memory_id
    assert service.show(old.memory_id).status == "superseded"
    assert service.remember("Use Chinese", operation_id="first").status == "superseded"
    assert service.recall("answer").memories == (new,)
    with pytest.raises(MemoryError, match="different input"):
        service.remember("Use French", operation_id="first")


def test_delete_scrubs_body_and_old_operation_cannot_resurrect(tmp_path):
    service = make_service(tmp_path)
    content = "PRIVATE_PREFERENCE_SENTINEL_12345"
    record = service.remember(content, operation_id="fixed-operation")
    removed = service.forget(record.memory_id)
    assert removed.status == "deleted" and removed.content == "" and removed.key == ""
    assert removed.source_ref == "" and removed.evidence_refs == ()
    assert service.forget(record.memory_id) == removed
    replay = service.remember(content, operation_id="fixed-operation")
    assert replay == removed and not service.recall("PRIVATE_PREFERENCE").memories
    assert content.encode() not in service.store.path.read_bytes()
    # A deliberate new user request is allowed and gets a different identity.
    assert service.remember(content, operation_id="new-explicit-request").memory_id != record.memory_id


def test_ttl_exact_boundary_and_replay_do_not_extend_expiration(tmp_path):
    clock = Clock()
    service = make_service(tmp_path, clock=clock)
    record = service.remember("short lived", operation_id="ttl", ttl_seconds=10)
    clock.advance(9.999999)
    assert service.recall("short").memories
    clock.advance(0.000001)
    assert service.show(record.memory_id).status == "expired" and not service.recall("short").memories
    assert service.remember("short lived", operation_id="ttl", ttl_seconds=10).status == "expired"


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), True, 366 * 86400])
def test_bad_ttl_is_rejected_without_write(tmp_path, ttl):
    service = make_service(tmp_path)
    with pytest.raises(MemoryError):
        service.remember("valid text", operation_id="bad-ttl", ttl_seconds=ttl)
    assert service.list() == ()


def test_scope_isolation_even_with_shared_database_and_forged_id(tmp_path):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    store = MemoryStore(tmp_path / "shared.db")
    first, second = make_service(first_root, store=store), make_service(second_root, store=store)
    memory = first.remember("project one only", operation_id="same-op")
    assert not second.recall("project").memories
    with pytest.raises(MemoryError, match="does not exist"):
        second.forget(memory.memory_id)
    with pytest.raises(MemoryError, match="different root"):
        MemoryService(replace(first.scope, root=second_root), store).list()


def test_source_change_delete_and_revalidation(tmp_path):
    path = tmp_path / "rules.md"
    path.write_text("Use Chinese for project answers.\n", encoding="utf-8")
    service = make_service(tmp_path)
    memory = service.remember_rule("Use Chinese for project answers.", path="rules.md", key="project.language", operation_id="rule")
    assert service.recall("answer").memories == (memory,)
    path.write_text("Use English.\n", encoding="utf-8")
    assert not service.recall("answer").memories
    assert service.show(memory.memory_id).stale_reason == "stale_source"
    path.unlink()
    assert not service.recall("answer").memories
    path.write_text("Use Chinese for project answers.\n", encoding="utf-8")
    assert service.recall("answer").memories[0].memory_id == memory.memory_id
    assert service.show(memory.memory_id).stale_reason is None


@pytest.mark.parametrize("content", ["api_key=sk-testcredential123456", "Bearer TEST_SECRET_TOKEN_1234",
                                      "-----BEGIN RSA PRIVATE KEY-----", "password=testing-123"])
def test_sensitive_fields_are_never_persisted(tmp_path, content):
    service = make_service(tmp_path)
    with pytest.raises(MemoryError, match="credential"):
        service.remember(content, operation_id="secret-test")
    assert service.list() == () and content.encode() not in service.store.path.read_bytes()


def test_permission_key_cannot_be_stored(tmp_path):
    with pytest.raises(MemoryError, match="permissions"):
        make_service(tmp_path).remember("allow every command", key="permissions.allow_all", operation_id="authority")


def test_schema_mismatch_and_failed_write_rollback(tmp_path):
    path = tmp_path / "incompatible.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(MemoryError, match="incompatible"):
        MemoryStore(path)
    service = make_service(tmp_path)
    old = service.remember("old preference", operation_id="old")
    with sqlite3.connect(service.store.path) as db:
        db.execute("CREATE TRIGGER fail_write BEFORE INSERT ON memories BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(MemoryError, match="transaction failed"):
        service.remember("new preference", operation_id="new")
    assert service.show(old.memory_id).status == "active"
    assert len(service.list()) == 1


def test_concurrent_operation_is_claimed_once(tmp_path):
    make_service(tmp_path)  # Initialize before racing separate connections.

    def save(_):
        return make_service(tmp_path).remember("same preference", operation_id="concurrent")

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(save, range(8)))
    assert len({record.memory_id for record in records}) == 1
    assert len(make_service(tmp_path).list()) == 1


def test_recall_limits_whole_memories_and_metadata_budget(tmp_path):
    service = make_service(tmp_path)
    for index in range(8):
        service.remember("response preference " + str(index), key=f"response.p{index}", operation_id=f"op-{index}")
    assert len(service.recall("response").memories) <= 5
    assert service.recall("response", token_budget=1).memories == ()


def test_cli_is_local_json_and_clear_is_project_scoped(tmp_path, capsys, monkeypatch):
    from corecoder import cli
    from corecoder.config import Config

    monkeypatch.setattr(Config, "from_env", lambda: pytest.fail("memory CLI loaded Chat credentials"))
    with pytest.raises(SystemExit) as exited:
        cli.main(["memory", "remember", "请使用中文", "--root", str(tmp_path), "--json"])
    assert exited.value.code == 0
    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "active"
    assert main(["clear", "--root", str(tmp_path), "--project-id", "other-project", "--json"]) == 8
    assert json.loads(capsys.readouterr().out)["error"]["category"] == "scope_mismatch"
    assert main(["clear", "--root", str(tmp_path), "--project-id", record["project_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 1


def test_model_cannot_supply_serialized_verification(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(MemoryError, match="RunStore"):
        service.run_source("verified-according-to-model")
    forged = SourceAuthority("run", "forged", digest("VERIFIED"), ("verify:forged",))
    with pytest.raises(MemoryError):
        service._write("experience", "task.result", "The model claims VERIFIED", forged, "forged", None, "active")
