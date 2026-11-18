"""Actual retriever/Prompt answers, bounded freshness recovery and no-evidence exits."""

import json

import pytest

pytest.importorskip("chromadb")
pytest.importorskip("langchain_chroma")

from corecoder.integrations.langchain_bridge import StructuredOutputError, StructuredPromptChain
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.answering import RepositoryAnswerer
from corecoder.knowledge.cli import ask_main, main
from corecoder.knowledge.loaders import read_source
from corecoder.llm import LLMResponse
from tests.knowledge.test_indexing import repository, service_for


class AnswerBackend:
    model = "scripted-repository-answer"

    def __init__(self, *, rewrite="RunStore", mutate=None, invalid=False):
        self.calls = 0
        self.rewrite = rewrite
        self.mutate = mutate
        self.invalid = invalid

    def chat_once(self, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        data = payload["input_data"]
        if "query" in payload["output_schema"]["properties"]:
            answer = {"query": self.rewrite, "unknowns": []}
        else:
            if self.mutate:
                self.mutate(self.calls)
            answer = {"status": "answered", "answer": "RunStore journals execution.", "unknowns": [],
                      "evidence_ids": ["invented-id"] if self.invalid else [data["evidence"][0]["id"]]}
        return LLMResponse(content=json.dumps(answer))


def test_repository_answer_uses_real_retrieval_and_program_generated_sources(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        service.index()
        backend = AnswerBackend()
        gateway = ModelGateway(backend, service.embeddings.ledger)
        answer = RepositoryAnswerer(service, gateway).ask("RunStore")
        assert answer.status == "answered" and answer.sources
        source = answer.sources[0]
        doc = read_source(scope, source["source_path"])
        assert doc.raw_hash == source["raw_hash"]
        assert 1 <= source["start_line"] <= source["end_line"] <= len(doc.text.splitlines())
        assert answer.snapshot_id in source["citation"]
        assert backend.calls == 1


def test_no_evidence_can_refuse_without_any_chat_request(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        service.index()
        backend = AnswerBackend()
        result = RepositoryAnswerer(service, ModelGateway(backend, service.embeddings.ledger), mode="lexical",
                                    max_query_rewrites=0).ask("ImaginaryQuantumPlanner")
        assert result.status == "insufficient" and not result.sources
        assert backend.calls == 0


def test_no_evidence_has_at_most_one_typed_query_rewrite(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        service.index()
        backend = AnswerBackend(rewrite="RunStore")
        gateway = ModelGateway(backend, service.embeddings.ledger)
        result = RepositoryAnswerer(service, gateway, mode="lexical").ask("ImaginaryQuantumPlanner")
        assert result.status == "answered"
        assert [record.operation for record in gateway.records] == ["query_rewrite", "answer"]
        assert backend.calls == 2


def test_unknown_model_evidence_is_never_accepted_after_repair(tmp_path):
    scope = repository(tmp_path)
    with service_for(scope) as service:
        service.index()
        backend = AnswerBackend(invalid=True)
        with pytest.raises(StructuredOutputError):
            RepositoryAnswerer(service, ModelGateway(backend, service.embeddings.ledger), mode="lexical").ask("RunStore")
        assert backend.calls == 2


@pytest.mark.parametrize("always_mutate", [False, True])
def test_edit_during_answer_refreshes_once_then_rejects_still_stale_sources(tmp_path, always_mutate):
    scope = repository(tmp_path)
    path = tmp_path / "corecoder/runtime.py"

    def mutate(call):
        if call == 1 or always_mutate:
            path.write_text(path.read_text(encoding="utf-8") + f"\n# version {call}\n", encoding="utf-8")

    with service_for(scope) as service:
        first = service.index()
        backend = AnswerBackend(mutate=mutate)
        result = RepositoryAnswerer(service, ModelGateway(backend, service.embeddings.ledger), mode="lexical").ask("RunStore")
        assert backend.calls == 2
        if always_mutate:
            assert result.status == "stale_evidence" and result.sources == () and result.answer == ""
        else:
            assert result.status == "answered" and result.snapshot_id != first.snapshot_id
            assert result.sources[0]["raw_hash"] == read_source(scope, "corecoder/runtime.py").raw_hash


def test_plan_nested_evidence_references_are_also_checked():
    class PlanBackend:
        model = "plan-fixture"

        def chat_once(self, *args, **kwargs):
            return LLMResponse(content=json.dumps({"task_type": "repo_qa", "goal": "Explain", "evidence_ids": [],
                "steps": [{"id": "s1", "action": "Explain", "expected_evidence": ["forged-nested-id"]}],
                "unknowns": [], "requires_code_change": False}))

    with pytest.raises(StructuredOutputError):
        StructuredPromptChain(ModelGateway(PlanBackend()), "task_plan.v2").invoke(
            {"goal": "Explain", "evidence": [], "constraints": [], "capabilities": []})


def test_local_memory_influences_answer_and_deletion_during_generation_discards_draft(tmp_path):
    from corecoder.memory.service import MemoryService

    scope = repository(tmp_path)
    memories = MemoryService(scope)
    preference = memories.remember("Use Chinese", key="response.language", operation_id="language")

    class LanguageBackend:
        model = "language-fixture"

        def __init__(self):
            self.calls = 0
            self.delete_during_generation = False

        def chat_once(self, messages, **kwargs):
            self.calls += 1
            data = json.loads(messages[-1]["content"])["input_data"]
            use_chinese = any(item["content"] == "Use Chinese" for item in data["memories"])
            if self.delete_during_generation and data["memories"]:
                memories.forget(preference.memory_id)
            return LLMResponse(content=json.dumps({"status": "answered", "answer": "已找到" if use_chinese else "Located",
                "evidence_ids": [data["evidence"][0]["id"]], "unknowns": []}))

    with service_for(scope) as service:
        service.index()
        backend = LanguageBackend()
        answerer = RepositoryAnswerer(service, ModelGateway(backend, service.embeddings.ledger), mode="lexical",
                                     memory_service=memories)
        assert answerer.ask("RunStore").answer == "已找到"
        assert answerer.ask("RunStore", current_memory_keys=("response.language",)).answer == "Located"
        backend.delete_during_generation = True
        assert answerer.ask("RunStore").answer == "Located"
        assert backend.calls == 4


def test_cli_fixture_returns_parseable_json_and_does_not_load_chat_config(tmp_path, monkeypatch, capsys):
    repository(tmp_path)
    from corecoder.config import Config

    monkeypatch.setattr(Config, "from_env", lambda: pytest.fail("offline fixture must not load Chat credentials"))
    assert main(["index", "--root", str(tmp_path), "--embedding-provider", "fixture", "--json"]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["mode"] == "fixture"
    assert ask_main(["RunStore", "--root", str(tmp_path), "--offline-fixture", "--sources", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fixture"] and result["status"] == "answered" and result["sources"]
    assert ask_main(["RunStore", "--root", str(tmp_path), "--offline-fixture"]) == 0
    human = capsys.readouterr().out
    assert "status=answered" in human and "mode=hybrid" in human and "backend=offline_fixture" in human
    assert "requests=" in human and "provider_cost_usd=unavailable" in human
