"""Bounded model proposals. These types cannot grant runtime authority."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ShortText = Annotated[str, Field(min_length=1, max_length=2000)]
EvidenceId = Annotated[str, Field(min_length=1, max_length=200)]
EvidenceIds = Annotated[list[EvidenceId], Field(max_length=32)]
Unknowns = Annotated[list[ShortText], Field(max_length=16)]


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def evidence_ids_used(self) -> set[str]:
        return set()


class RepoAnswer(Proposal):
    status: Literal["answered", "insufficient"]
    answer: Annotated[str, Field(max_length=12000)]
    evidence_ids: EvidenceIds
    unknowns: Unknowns

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "answered" and (not self.evidence_ids or not self.answer.strip()):
            raise ValueError("answered requires supporting evidence and nonempty text")
        if self.status == "insufficient" and not self.unknowns:
            raise ValueError("insufficient requires an explanation in unknowns")
        return self

    def evidence_ids_used(self) -> set[str]:
        return set(self.evidence_ids)


class PlanStep(Proposal):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    action: ShortText
    expected_evidence: EvidenceIds


class TaskPlan(Proposal):
    task_type: Literal["repo_qa", "code_change", "insufficient"]
    goal: ShortText
    evidence_ids: EvidenceIds
    steps: Annotated[list[PlanStep], Field(max_length=12)]
    unknowns: Unknowns
    requires_code_change: bool

    @model_validator(mode="after")
    def consistent_plan(self):
        if self.requires_code_change != (self.task_type == "code_change"):
            raise ValueError("requires_code_change must agree with task_type")
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("step ids must be unique")
        if self.task_type == "insufficient" and not self.unknowns:
            raise ValueError("insufficient requires unknowns")
        if self.task_type != "insufficient" and not self.steps:
            raise ValueError("an actionable plan requires steps")
        return self

    def evidence_ids_used(self) -> set[str]:
        # expected_evidence is an existing evidence-ID list. Future evidence
        # requirements belong in action/unknowns, not in this reference field.
        return set(self.evidence_ids) | {ref for step in self.steps for ref in step.expected_evidence}


class MemoryCandidate(Proposal):
    kind: Literal["preference", "experience"]
    key: Annotated[str, Field(min_length=1, max_length=128)]
    content: ShortText
    source_ref: Annotated[str, Field(min_length=1, max_length=200)]
    evidence_ids: EvidenceIds


class MemoryExtraction(Proposal):
    candidates: Annotated[list[MemoryCandidate], Field(max_length=5)]

    def evidence_ids_used(self) -> set[str]:
        return {ref for candidate in self.candidates for ref in candidate.evidence_ids}


class QueryRewrite(Proposal):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    unknowns: Unknowns


OUTPUT_SCHEMAS = {"repo_answer": RepoAnswer, "task_plan": TaskPlan, "memory_extract": MemoryExtraction, "query_rewrite": QueryRewrite}
