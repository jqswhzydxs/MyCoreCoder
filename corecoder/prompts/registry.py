"""Load only known package resources, with stable hashes and input contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files

_VARIABLES = {
    "repo_answer": frozenset({"question", "evidence", "memories"}),
    "task_plan": frozenset({"goal", "evidence", "constraints", "capabilities"}),
    "memory_extract": frozenset({"source", "source_ref", "evidence"}),
    "query_rewrite": frozenset({"question", "evidence"}),
}


@dataclass(frozen=True)
class PromptTemplate:
    template_id: str
    text: str
    sha256: str
    variables: frozenset[str]

    def payload(self, values: dict, *, schema: dict, repair_errors: list[dict] | None = None) -> str:
        if set(values) != self.variables:
            raise ValueError("prompt variables must match the template input contract")
        return json.dumps({
            "input_data": values,
            "output_schema": schema,
            "repair_errors": repair_errors or [],
        }, ensure_ascii=False, sort_keys=True)


def load_prompt(template_id: str) -> PromptTemplate:
    parts = template_id.split(".")
    if len(parts) != 2 or parts[0] not in _VARIABLES or parts[1] not in {"v1", "v2"}:
        raise ValueError("unknown prompt template")
    text = files("corecoder.prompts").joinpath(f"{template_id}.txt").read_text(encoding="utf-8")
    return PromptTemplate(template_id, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), _VARIABLES[parts[0]])
