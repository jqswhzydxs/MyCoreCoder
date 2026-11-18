"""Local commands must route before Chat configuration and preserve legacy CLI."""

import importlib.util
import json
from unittest.mock import Mock

import pytest

from corecoder import cli
from corecoder.knowledge.cli import main
from corecoder.knowledge.models import KnowledgeError
from corecoder.knowledge.settings import load_settings


def test_local_status_does_not_construct_chat_clients_or_create_an_index(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.Config, "from_env", Mock(side_effect=AssertionError("Chat config is not needed")))
    with pytest.raises(SystemExit) as exit_code:
        cli.main(["knowledge", "status", "--root", str(tmp_path), "--json"])
    assert exit_code.value.code == 0
    assert json.loads(capsys.readouterr().out)["indexed"] is False
    assert not (tmp_path / ".corecoder").exists()


def test_missing_index_returns_structured_error_without_chat_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.Config, "from_env", Mock(side_effect=AssertionError("Chat config is not needed")))
    assert main(["search", "RunStore", "--mode", "lexical", "--root", str(tmp_path), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["category"] == "index_missing"


def test_old_one_shot_and_resume_arguments_remain_compatible():
    assert cli._parse_args(["-p", "hello", "--yes"]).prompt == "hello"
    assert cli._parse_args(["--resume-run", "run-1"]).resume_run == "run-1"
    with pytest.raises(SystemExit) as exc:
        cli._parse_args(["unknown-command"])
    assert exc.value.code == 2


def test_settings_priority_is_cli_then_environment_then_project(tmp_path, monkeypatch):
    (tmp_path / "corecoder.toml").write_text('[knowledge]\nchunk_size=1200\n[embedding]\nmodel="project-model"\n', encoding="utf-8")
    monkeypatch.setenv("CORECODER_KNOWLEDGE_CHUNK_SIZE", "1600")
    monkeypatch.setenv("CORECODER_EMBEDDING_MODEL", "environment-model")
    assert load_settings({"root": str(tmp_path), "chunk_size": 2000}).chunk_size == 2000
    assert load_settings({"root": str(tmp_path)}).chunk_size == 1600
    assert load_settings({"root": str(tmp_path)}).embedding.model == "environment-model"
    monkeypatch.delenv("CORECODER_KNOWLEDGE_CHUNK_SIZE")
    assert load_settings({"root": str(tmp_path)}).chunk_size == 1200


def test_compatible_provider_batch_limits_are_applied_and_budgeted(tmp_path, monkeypatch):
    (tmp_path / "corecoder.toml").write_text(
        '[embedding]\nprovider="fixture"\nbatch_size=10\nmax_text_bytes=8192\nmax_batch_bytes=81920\n', encoding="utf-8")
    settings = load_settings({"root": str(tmp_path)})
    gateway = settings.gateway(None)
    assert gateway.batch_size == 10 and gateway.max_text_bytes == 8192
    assert len(gateway.embed_documents(["sample"] * 21)) == 21
    assert gateway.ledger.snapshot()["requests"] == 3
    monkeypatch.setenv("CORECODER_EMBEDDING_BATCH_SIZE", "5")
    assert load_settings({"root": str(tmp_path)}).embedding_batch_size == 5
    assert load_settings({"root": str(tmp_path), "batch_size": 2}).embedding_batch_size == 2
    for values in ({"batch_size": 33}, {"max_text_bytes": 8192, "max_batch_bytes": 100}):
        with pytest.raises(KnowledgeError, match="invalid values"):
            load_settings({"root": str(tmp_path), **values})


@pytest.mark.skipif(importlib.util.find_spec("pathspec") is None, reason="optional knowledge loader dependency is absent")
def test_dry_run_does_not_read_credentials_call_models_or_create_storage(tmp_path, monkeypatch, capsys):
    (tmp_path / "corecoder").mkdir()
    (tmp_path / "corecoder/a.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(cli.Config, "from_env", Mock(side_effect=AssertionError("Chat config is not needed")))
    assert main(["index", "--root", str(tmp_path), "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["files"] == ["corecoder/a.py"]
    assert result["embedding_requests"] == 0
    assert not (tmp_path / ".corecoder").exists()
