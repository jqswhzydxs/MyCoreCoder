"""Repository answers from actual retrieval, bounded repair and fresh sources."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..integrations.langchain_bridge import StructuredPromptChain, _local_tracing
from ..integrations.model_gateway import ModelGateway
from ..llm import LLMResponse
from .citations import build_evidence, render_sources, validate_hit
from .models import SearchResult
from .retriever import knowledge_retriever
from .service import KnowledgeService


@dataclass(frozen=True)
class AnswerResult:
    status: str
    answer: str
    sources: tuple[dict, ...]
    snapshot_id: str
    mode: str
    degraded: tuple[str, ...]
    unknowns: tuple[str, ...]
    budget: dict
    model: str
    prompt_id: str
    prompt_sha256: str
    fixture: bool

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


class FixtureAnswerBackend:
    """Extractive offline demonstration, never a live-model quality result."""

    model = "extractive-fixture-v1"

    def chat_once(self, messages, *, tools, timeout_seconds, max_tokens):
        from .lexical_store import tokenize

        payload = json.loads(messages[-1]["content"])
        data = payload["input_data"]
        if "query" in payload["output_schema"]["properties"]:
            value = {"query": data["question"], "unknowns": []}
        else:
            query_tokens = set(tokenize(data["question"]))
            evidence = [item for item in data["evidence"] if query_tokens.intersection(tokenize(item["text"]))]
            explicit_identifiers = re.findall(r"\b[A-Z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", data["question"])
            supplied = " ".join(item["text"] + " " + item.get("symbol", "") for item in data["evidence"])
            if any(identifier not in supplied for identifier in explicit_identifiers):
                evidence = []
            value = {"status": "answered" if evidence else "insufficient",
                     "answer": evidence[0]["text"][:1000].strip() if evidence else "",
                     "evidence_ids": [evidence[0]["id"]] if evidence else [],
                     "unknowns": [] if evidence else ["The fixture cannot establish support from the supplied evidence."]}
        return LLMResponse(content=json.dumps(value, ensure_ascii=False))


class RepositoryAnswerer:
    def __init__(self, service: KnowledgeService, gateway: ModelGateway, *, mode: str = "hybrid", top_k: int = 6,
                 context_token_budget: int = 4000, max_query_rewrites: int = 1, template_id: str = "repo_answer.v2",
                 memory_service=None):
        if max_query_rewrites not in {0, 1}:
            raise ValueError("at most one query rewrite is supported")
        if service.embeddings and service.embeddings.ledger is not gateway.ledger:
            raise ValueError("retrieval and generation must share the same budget ledger")
        self.service, self.gateway = service, gateway
        self.mode, self.top_k = mode, top_k
        self.context_token_budget = context_token_budget
        self.max_query_rewrites = max_query_rewrites
        self.chain = StructuredPromptChain(gateway, template_id)
        self.memory_service = memory_service

    def retrieve(self, query: str) -> SearchResult:
        retriever = knowledge_retriever(self.service, mode=self.mode, top_k=self.top_k)
        with _local_tracing():
            documents = retriever.invoke(query, config={"callbacks": []})
        result = retriever.last_result
        mapping = {hit.chunk.chunk_id: hit for hit in result.hits}
        return SearchResult(result.snapshot_id, result.requested_mode, result.mode,
                            tuple(mapping[document.metadata["chunk_id"]] for document in documents), result.degraded, result.excluded)

    def ask(self, question: str, *, memories: list[dict] | None = None,
            current_memory_keys: tuple[str, ...] = (), initial_result: SearchResult | None = None) -> AnswerResult:
        query, rewrites, refreshed, memory_refreshed = question, 0, False, False
        while True:
            self.gateway.ledger.check()
            result = initial_result if initial_result is not None else self.retrieve(query)
            initial_result = None
            bundle = build_evidence(self.service.scope, question, result, token_budget=self.context_token_budget,
                                    max_file_bytes=self.service.max_file_bytes)
            stale = [item for item in bundle.excluded if item["reason"] != "context_budget"]
            if stale and not refreshed and (self.mode == "lexical" or self.service.embeddings):
                self.service.index(mode=self.mode)
                refreshed = True
                continue
            if not bundle.evidence:
                if stale:
                    return self._result("stale_evidence", "", bundle, [], ["Repository sources changed; no current evidence could be established."])
                if rewrites < self.max_query_rewrites:
                    rewrite = StructuredPromptChain(self.gateway, "query_rewrite.v2").invoke({"question": question, "evidence": []})
                    rewrites += 1
                    if rewrite.value.query != query:
                        query = rewrite.value.query
                        continue
                return self._result("insufficient", "", bundle, [], ["No repository evidence supports this question."])
            recalled = (self.memory_service.recall(question, current_keys=current_memory_keys).prompt_data()
                        if self.memory_service else memories or [])
            proposal = self.chain.invoke({"question": question, "evidence": [item.prompt_data() for item in bundle.evidence],
                                          "memories": recalled}).value
            if self.memory_service and recalled != self.memory_service.recall(question, current_keys=current_memory_keys).prompt_data():
                if not memory_refreshed:
                    memory_refreshed = True
                    continue
                return self._result("insufficient", "", bundle, [], ["Memory changed during generation; the draft was discarded."])
            used = {item.id: item for item in bundle.evidence if item.id in proposal.evidence_ids}
            invalid = any(validate_hit(self.service.scope, item.hit, max_file_bytes=self.service.max_file_bytes) for item in used.values())
            if invalid:
                if not refreshed and (self.mode == "lexical" or self.service.embeddings):
                    self.service.index(mode=self.mode)
                    refreshed = True
                    continue
                return self._result("stale_evidence", "", bundle, [], ["Sources changed during generation; the draft answer was discarded."])
            return self._result(proposal.status, proposal.answer, bundle, proposal.evidence_ids, proposal.unknowns)

    def _result(self, status, answer, bundle, ids, unknowns) -> AnswerResult:
        return AnswerResult(status, answer, tuple(render_sources(bundle, ids)), bundle.snapshot_id, bundle.mode, bundle.degraded,
                            tuple(unknowns), self.gateway.ledger.snapshot(), self.gateway.backend.model,
                            self.chain.template.template_id, self.chain.template.sha256,
                            isinstance(self.gateway.backend, FixtureAnswerBackend))
