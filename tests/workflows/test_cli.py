"""Graph CLI uses stable IDs and emits one JSON value without reading live keys."""

import json

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("chromadb")

from corecoder import cli
from corecoder.config import Config
from corecoder.knowledge.cli import ask_main
from corecoder.workflows.cli import main
from tests.workflows.test_graph import repository


def test_graph_ask_status_and_resume_keep_identity_and_budget(tmp_path, monkeypatch, capsys):
    repository(tmp_path)
    monkeypatch.setattr(Config, "from_env", lambda: pytest.fail("fixture/status must not load Chat credentials"))
    assert ask_main(["RunStore", "--root", str(tmp_path), "--engine", "graph", "--offline-fixture", "--mode", "lexical",
                     "--thread-id", "thread", "--turn-id", "turn", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["fixture"] and first["workflow_status"] == "completed" and first["result"]["sources"]
    with pytest.raises(SystemExit) as stopped:
        cli.main(["workflow", "status", first["workflow_id"], "--root", str(tmp_path), "--json"])
    assert stopped.value.code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["workflow_id"] == first["workflow_id"] and status["budget"] == first["budget"]
    assert status["fixture"]
    assert main(["resume", first["workflow_id"], "--root", str(tmp_path), "--offline-fixture", "--json"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["workflow_id"] == first["workflow_id"] and resumed["budget"] == first["budget"]


def test_graph_run_uses_only_named_project_verification_and_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Config, "from_env", lambda: pytest.fail("fixture must not load Chat credentials"))
    (tmp_path / "artifact.md").write_text("ready", encoding="utf-8")
    (tmp_path / "corecoder.toml").write_text('[workflow.verification]\npolicy_id="artifact-v1"\ntype="file_state"\n'
                                           'path="artifact.md"\ncontent_equals="ready"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["run", "Check artifact", "--engine", "graph", "--root", str(tmp_path), "--offline-fixture", "--mode", "lexical", "--json"])
    assert stopped.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fixture"] and result["code_run_status"] == "verified" and result["answer_status"] == "not_started"
    assert result["result"]["evidence"][0]["verifier"] == "file_state"
