"""Only references and content-free control metadata belong in checkpoints."""

from __future__ import annotations

from typing import TypedDict


class WorkflowError(RuntimeError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


class WorkflowState(TypedDict, total=False):
    schema_version: int
    workflow_id: str
    owner_id: str
    project_id: str
    thread_id: str
    turn_id: str
    request_ref: str
    retrieval_ref: str
    memory_ref: str
    plan_ref: str
    result_ref: str
    snapshot_id: str
    attempt: int
    child_run_id: str
    budget: dict
    stage: str
    workflow_status: str
    answer_status: str
    code_run_status: str
    task_type: str
    configuration_hash: str
    memory_operation_id: str
    memory_status: str
    error_category: str
    fixture: bool
