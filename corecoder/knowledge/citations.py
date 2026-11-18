"""Validate current file hashes and render citations from trusted metadata."""

from __future__ import annotations

import json
from dataclasses import asdict

from ..context import estimate_tokens
from .loaders import read_source
from .models import EvidenceBundle, EvidenceItem, KnowledgeError, ProjectScope, SearchHit, SearchResult, digest


def validate_hit(scope: ProjectScope, hit: SearchHit, *, max_file_bytes: int = 1024 * 1024) -> str | None:
    chunk = hit.chunk
    if chunk.project_id != scope.project_id:
        return "scope_mismatch"
    try:
        document = read_source(scope, chunk.source_path, max_file_bytes=max_file_bytes)
    except KnowledgeError as exc:
        return exc.category
    lines = document.text.splitlines(keepends=True)
    if document.raw_hash != chunk.raw_hash:
        return "source_hash_changed"
    if chunk.start_line < 1 or chunk.end_line > len(lines):
        return "invalid_line_range"
    if "".join(lines[chunk.start_line - 1:chunk.end_line]) != chunk.text:
        return "source_span_changed"
    return None


def build_evidence(scope: ProjectScope, query: str, result: SearchResult, *, token_budget: int = 4000,
                   max_file_bytes: int = 1024 * 1024) -> EvidenceBundle:
    if token_budget < 1:
        raise ValueError("evidence token budget must be positive")
    evidence, excluded = [], list(result.excluded)
    used = 0
    for hit in result.hits:
        if hit.snapshot_id != result.snapshot_id:
            raise KnowledgeError("index_corrupt", "mixed snapshots cannot form an evidence bundle")
        invalid = validate_hit(scope, hit, max_file_bytes=max_file_bytes)
        if invalid:
            excluded.append({"chunk_id": hit.chunk.chunk_id, "reason": invalid})
            continue
        lines = hit.chunk.text.splitlines(keepends=True)
        body, end = "", hit.chunk.start_line - 1
        # Trim only on full source lines; keep the stored chunk id and expose
        # the narrower visible citation span as separate trusted metadata.
        for offset, line in enumerate(lines):
            candidate = body + line
            line_end = hit.chunk.start_line + offset
            ref = "e_" + digest([scope.identity(), result.snapshot_id, hit.chunk.chunk_id, line_end])[:24]
            item = EvidenceItem(ref, hit, candidate, line_end)
            cost = estimate_tokens([{"role": "user", "content": json.dumps(item.prompt_data(), ensure_ascii=False)}])
            if used + cost > token_budget:
                break
            body, end = candidate, line_end
        if not body.strip():
            excluded.append({"chunk_id": hit.chunk.chunk_id, "reason": "context_budget"})
            continue
        ref = "e_" + digest([scope.identity(), result.snapshot_id, hit.chunk.chunk_id, end])[:24]
        item = EvidenceItem(ref, hit, body, end)
        evidence.append(item)
        used += estimate_tokens([{"role": "user", "content": json.dumps(item.prompt_data(), ensure_ascii=False)}])
    return EvidenceBundle(digest(query), scope.owner_id, scope.project_id, result.snapshot_id, tuple(evidence),
                          result.mode, result.degraded, tuple(excluded))


def render_sources(bundle: EvidenceBundle, evidence_ids: list[str]) -> list[dict]:
    mapping = {item.id: item for item in bundle.evidence}
    if not set(evidence_ids).issubset(mapping):
        raise KnowledgeError("invalid_evidence", "answer contains unknown evidence ids")
    return [{"id": evidence_id, **asdict(mapping[evidence_id].citation), "citation": mapping[evidence_id].citation.render(),
             "source_kind": mapping[evidence_id].hit.chunk.source_kind, "symbol": mapping[evidence_id].hit.chunk.symbol}
            for evidence_id in dict.fromkeys(evidence_ids)]
