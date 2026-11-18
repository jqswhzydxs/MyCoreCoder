"""Model candidates require explicit review and real RunStore provenance."""

import json

import pytest

from corecoder import Agent, FileStateVerifier, RunStatus, RunStore
from corecoder.demo import ScriptedLLM
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.scope import create_scope
from corecoder.llm import LLMResponse
from corecoder.memory.models import MemoryError
from corecoder.memory.service import MemoryService

pytest.importorskip("langchain_core")


class CandidateBackend:
    model = "memory-candidate-fixture"

    def __init__(self, *, forged_source=False, kind="preference"):
        self.forged_source = forged_source
        self.kind = kind
        self.calls = 0

    def chat_once(self, messages, **kwargs):
        self.calls += 1
        data = json.loads(messages[-1]["content"])["input_data"]
        return LLMResponse(content=json.dumps({"candidates": [{"kind": self.kind,
            "key": "task.experience" if self.kind == "experience" else "response.language",
            "content": "The verified file check succeeded." if self.kind == "experience" else "Use Chinese",
            "source_ref": "forged-source" if self.forged_source else data["source_ref"],
            "evidence_ids": [item["id"] for item in data["evidence"]]}]}))


def test_model_output_stays_candidate_until_explicit_approval(tmp_path):
    service = MemoryService(create_scope(tmp_path))
    source = service.explicit_source("Remember to use Chinese", "user-turn")
    record, = service.stage_extraction(ModelGateway(CandidateBackend()), source_text="Remember to use Chinese",
                                       source=source, operation_id="extract")
    assert record.status == "candidate" and service.recall("Chinese").memories == ()
    assert service.approve(record.memory_id).status == "active"
    assert service.recall("Chinese").memories
    service.forget(record.memory_id)
    replay, = service.stage_extraction(ModelGateway(CandidateBackend()), source_text="Remember to use Chinese",
                                       source=source, operation_id="extract")
    assert replay.status == "deleted" and service.approve(record.memory_id).status == "deleted"


def test_forged_model_source_never_enters_storage(tmp_path):
    from corecoder.integrations.langchain_bridge import StructuredOutputError

    service = MemoryService(create_scope(tmp_path))
    source = service.explicit_source("Remember Chinese", "user")
    with pytest.raises(StructuredOutputError, match="source_reference_mismatch"):
        service.stage_extraction(ModelGateway(CandidateBackend(forged_source=True)), source_text="Remember Chinese",
                                 source=source, operation_id="bad-source")
    assert service.list() == ()


def test_actual_verified_agent_run_is_rechecked_for_experience(tmp_path):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("ready", encoding="utf-8")
    runs = RunStore(tmp_path / "runs.db")
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="The verified file check succeeded.")]), tools=[], run_store=runs)
    result = agent.run({"goal": "Check artifact", "verifier": FileStateVerifier(artifact, content_equals="ready")})
    assert result.status == RunStatus.VERIFIED
    service = MemoryService(create_scope(tmp_path), run_store=runs)
    source = service.run_source(result.run_id)
    record, = service.stage_extraction(ModelGateway(CandidateBackend(kind="experience")),
        source_text="The verified file check succeeded.", source=source, operation_id="experience")
    assert record.evidence_refs and record.status == "candidate"
    assert service.approve(record.memory_id).status == "active"
    assert service.recall("verified file").memories
    # The same model claim without a verifier is rejected.
    unverified = Agent(llm=ScriptedLLM([LLMResponse(content="VERIFIED")]), tools=[], run_store=runs).run("Check artifact")
    with pytest.raises(MemoryError, match="verification evidence"):
        service.run_source(unverified.run_id)


def test_source_changed_before_candidate_approval(tmp_path):
    path = tmp_path / "rules.md"
    path.write_text("Use Chinese", encoding="utf-8")
    service = MemoryService(create_scope(tmp_path))
    source = service.file_source("rules.md")
    record, = service.stage_extraction(ModelGateway(CandidateBackend()), source_text="Use Chinese", source=source, operation_id="file")
    path.write_text("Use English", encoding="utf-8")
    with pytest.raises(MemoryError, match="changed"):
        service.approve(record.memory_id)
    assert not service.recall("language").memories
