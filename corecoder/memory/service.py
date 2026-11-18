"""Review, recall and delete local memories using independently checked sources."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from ..context import estimate_tokens
from ..knowledge.lexical_store import tokenize
from ..knowledge.loaders import read_source
from ..knowledge.models import KnowledgeError, ProjectScope, digest, relative_path
from .models import MemoryError, MemoryRecord, RecallResult, SourceAuthority, bounded, timestamp, utc_now
from .policy import check_candidate, check_content
from .store import MemoryStore


class MemoryService:
    def __init__(self, scope: ProjectScope, store: MemoryStore | None = None, *, clock=utc_now, run_store=None):
        self.scope = scope
        self.store = store or MemoryStore(scope.root / ".corecoder/memory/memories.db")
        self.clock = clock
        # This is a trusted, project-bound RunStore supplied by the host. A
        # model cannot choose a database or pass a serialized VERIFIED state.
        self.run_store = run_store

    def _now(self) -> str:
        return timestamp(self.clock())

    def explicit_source(self, content: str, operation_id: str) -> SourceAuthority:
        check_content(content)
        return SourceAuthority("user", "user:" + digest(operation_id), digest(content))

    def file_source(self, path: str) -> SourceAuthority:
        try:
            relative_path(path)
            if Path(path).suffix.lower() not in {".py", ".md"}:
                raise ValueError("unsupported rule source")
            document = read_source(self.scope, path)
            check_content(document.text, maximum=1024 * 1024)
            return SourceAuthority("file", path, document.raw_hash, ("file:" + document.raw_hash,))
        except (KnowledgeError, ValueError):
            raise MemoryError("invalid_source", "project rule source is unavailable or outside this project") from None

    def run_source(self, run_id: str) -> SourceAuthority:
        if self.run_store is None or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id):
            raise MemoryError("unverified_source", "a trusted project RunStore is required for experience")
        from ..run_store import RunStoreError

        try:
            state = self.run_store.load_run(run_id).state
            evidence = state.get("evidence") or []
            refs = tuple(item["evidence_id"] for item in evidence)
            if (state.get("status") != "verified" or state.get("verification_status") != "passed"
                    or not refs or len(refs) != len(set(refs)) or any(item.get("passed") is not True for item in evidence)):
                raise ValueError("incomplete run evidence")
            from ..verification import VerificationEvidence

            for item in evidence:
                observed = VerificationEvidence.create(verifier=item["verifier"], kind=item["kind"], source=item["source"],
                    criterion=item["criterion"], passed=True, summary=item["summary"], metadata=item.get("metadata"))
                if observed.evidence_id != item["evidence_id"]:
                    raise ValueError("verification evidence identity mismatch")
            source_hash = digest({"run_id": run_id, "status": state["status"], "verification_status": state["verification_status"],
                                  "criteria": state.get("success_criteria", []), "evidence": evidence})
            return SourceAuthority("run", run_id, source_hash, refs)
        except (RunStoreError, ValueError, TypeError, KeyError):
            raise MemoryError("unverified_source", "experience source is missing or lacks complete trusted verification evidence") from None

    def _validate_source(self, source: SourceAuthority) -> None:
        if (not isinstance(source, SourceAuthority) or not re.fullmatch(r"[a-f0-9]{64}", source.source_hash)
                or len(source.evidence_refs) > 32):
            raise MemoryError("invalid_source", "invalid memory source attestation")
        if source.kind == "file":
            current = self.file_source(source.source_ref)
        elif source.kind == "run":
            current = self.run_source(source.source_ref)
        elif source.kind == "user" and re.fullmatch(r"user:[a-f0-9]{64}", source.source_ref):
            return
        else:
            raise MemoryError("invalid_source", "unknown memory source")
        if current != source:
            raise MemoryError("stale_source", "memory source changed; explicit revalidation is required")

    def remember(self, content: str, *, key: str = "response.preference", operation_id: str,
                 ttl_seconds: float | None = None) -> MemoryRecord:
        source = self.explicit_source(content, operation_id)
        return self._write("preference", key, content, source, operation_id, ttl_seconds, "active")

    def remember_rule(self, content: str, *, path: str, key: str, operation_id: str,
                      ttl_seconds: float | None = None) -> MemoryRecord:
        source = self.file_source(path)
        document = read_source(self.scope, path)
        if content not in document.text:
            raise MemoryError("invalid_source", "a deterministic project rule must be present in its source")
        return self._write("preference", key, content, source, operation_id, ttl_seconds, "active")

    def _write(self, kind, key, content, source, operation_id, ttl_seconds, status) -> MemoryRecord:
        operation_id = bounded(operation_id, 200, "operation identity")
        check_content(operation_id)
        self._validate_source(source)
        key, content = check_candidate(kind, key, content, source)
        if ttl_seconds is not None and (isinstance(ttl_seconds, bool) or not 0 < ttl_seconds <= 365 * 86400):
            raise MemoryError("invalid_request", "memory TTL must be positive and at most one year")
        now = self.clock()
        expires = timestamp(now + timedelta(seconds=ttl_seconds)) if ttl_seconds is not None else None
        request_hash = digest([kind, key, content, asdict(source), ttl_seconds, status])
        record = MemoryRecord(digest([self.scope.identity(), operation_id]), self.scope.owner_id, self.scope.project_id,
                              kind, key, content, source.kind, source.source_ref, source.source_hash, source.evidence_refs,
                              status, timestamp(now), expires, None, operation_id)
        self.store.write(self.scope, record, request_hash)
        return self.store.get(self.scope, record.memory_id, self._now())

    def stage_extraction(self, gateway, *, source_text: str, source: SourceAuthority, operation_id: str,
                         ttl_seconds: float | None = None) -> tuple[MemoryRecord, ...]:
        """The caller supplies provenance. Model output is staged, never activated."""
        from ..integrations.langchain_bridge import StructuredPromptChain

        check_content(source_text, maximum=64 * 1024)
        self._validate_source(source)
        if source.kind == "user" and digest(source_text) != source.source_hash:
            raise MemoryError("invalid_source", "explicit request does not match its source hash")
        if source.kind == "file" and source_text != read_source(self.scope, source.source_ref).text:
            raise MemoryError("invalid_source", "rule extraction must use the actual source document")
        if source.kind == "run":
            # Run summaries must come from the trusted record, never arbitrary
            # text packaged beside an otherwise legitimate run identity.
            state = self.run_store.load_run(source.source_ref).state
            trusted = state.get("model_text", state.get("final_text", ""))
            if source_text != trusted:
                raise MemoryError("invalid_source", "experience extraction must use the stored run summary")
        proposal = StructuredPromptChain(gateway, "memory_extract.v2").invoke({"source": source_text,
            "source_ref": source.source_ref, "evidence": [{"id": ref} for ref in source.evidence_refs]}).value
        for item in proposal.candidates:
            if (item.source_ref != source.source_ref or not set(item.evidence_ids).issubset(source.evidence_refs)
                    or (source.kind in {"run", "file"} and not item.evidence_ids)):
                raise MemoryError("invalid_source", "candidate provenance does not match the trusted source")
            check_candidate(item.kind, item.key, item.content, source)
        return tuple(self._write(item.kind, item.key, item.content, source, f"{operation_id}:{index}", ttl_seconds, "candidate")
                     for index, item in enumerate(proposal.candidates))

    def approve(self, memory_id: str) -> MemoryRecord:
        record = self.show(memory_id)
        if record.status != "candidate":
            return record
        self._validate_source(SourceAuthority(record.source_kind, record.source_ref, record.source_hash, record.evidence_refs))
        check_candidate(record.kind, record.key, record.content,
                        SourceAuthority(record.source_kind, record.source_ref, record.source_hash, record.evidence_refs))
        self.store.activate(self.scope, memory_id, self._now())
        return self.show(memory_id)

    def list(self) -> tuple[MemoryRecord, ...]:
        return self.store.records(self.scope, self._now())

    def show(self, memory_id: str) -> MemoryRecord:
        return self.store.get(self.scope, memory_id, self._now())

    def forget(self, memory_id: str) -> MemoryRecord:
        # No in-memory recall cache exists. Every recall rechecks the database.
        return self.store.forget(self.scope, memory_id, self._now())

    def plan_constraints(self, query: str, current: list, *, current_keys: tuple[str, ...] = ()) -> list:
        """Append labelled historical data; current constraints stay first."""
        return [*current, *self.recall(query, current_keys=current_keys).constraints()]

    def recall(self, query: str, *, current_keys: tuple[str, ...] = (), max_memories: int = 5,
               token_budget: int = 1000) -> RecallResult:
        if not 0 <= max_memories <= 5 or not 0 <= token_budget <= 1000:
            raise MemoryError("invalid_request", "memory recall is limited to five records and 1000 tokens")
        tokens = set(tokenize(query))
        selected, excluded = [], []
        for record in self.list():
            reason = None
            if record.status != "active":
                reason = record.status
            elif record.key in current_keys:
                reason = "current_instruction_overrides_history"
            else:
                try:
                    self._validate_source(SourceAuthority(record.source_kind, record.source_ref,
                                                          record.source_hash, record.evidence_refs))
                except MemoryError as exc:
                    reason = exc.category
                    self.store.set_stale(self.scope, record.memory_id, reason)
                else:
                    if record.stale_reason:
                        self.store.set_stale(self.scope, record.memory_id, None)
            if reason:
                excluded.append({"id": record.memory_id, "key": record.key, "reason": reason})
                continue
            score = len(tokens.intersection(tokenize(record.key + " " + record.content)))
            if score or record.kind == "preference":
                selected.append((score, record.source_kind == "file", record.created_at, record))
        selected.sort(key=lambda item: item[:3], reverse=True)
        result = []
        for *_, record in selected:
            proposed = [item.prompt_data() for item in [*result, record]]
            # Include metadata overhead, not just content length. Never truncate
            # a preference into a different instruction.
            cost = max(1, estimate_tokens([{"content": json.dumps(proposed, ensure_ascii=False)}]))
            if len(result) >= max_memories or cost > token_budget:
                excluded.append({"id": record.memory_id, "key": record.key, "reason": "context_budget"})
            else:
                result.append(record)
        return RecallResult(tuple(result), tuple(excluded))
