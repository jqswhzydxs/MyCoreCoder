"""No network/keys: crash after one real file write, resume the same graph child."""

import json
import tempfile
from pathlib import Path

from corecoder import Agent, FileStateVerifier, RunStore
from corecoder.integrations.agent_llm_bridge import AgentLLMBridge
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import FixtureEmbeddingProvider
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.run_store import InjectedProcessCrash
from corecoder.tools.write import WriteFileTool
from corecoder.workflows.fixtures import FixtureWorkflowBackend
from corecoder.workflows.graph import GraphWorkflow


class RecoveryBackend(FixtureWorkflowBackend):
    def __init__(self, target, *, request_write):
        super().__init__("code")
        self.target, self.request_write = target, request_write

    def chat_once(self, messages, **kwargs):
        try:
            json.loads(messages[-1]["content"])
        except (ValueError, TypeError):
            if self.request_write:
                self.request_write = False
                return LLMResponse(tool_calls=[ToolCall("write-artifact", "write_file", {"file_path": str(self.target), "content": "ready"})])
            return LLMResponse(content="The artifact is ready for the trusted file verifier.")
        return super().chat_once(messages, **kwargs)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="corecoder-graph-resume-") as directory:
        root = Path(directory)
        scope, target = create_scope(root), root / "artifact.md"
        writes = []
        runs = RunStore(root / ".corecoder/runs.db")

        class CountedWrite(WriteFileTool):
            def execute(self, **kwargs):
                writes.append(1)
                return super().execute(**kwargs)

        def crash(boundary, metadata):
            if boundary == "tool_after_execute_before_receipt":
                raise InjectedProcessCrash("demo crash after file write and before receipt")

        def build(*, request_write, crash_hook=None):
            backend = RecoveryBackend(target, request_write=request_write)

            def agent_factory(gateway, cancellation):
                return Agent(AgentLLMBridge(gateway, cancellation_token=cancellation), tools=[CountedWrite()],
                             permission=Permission(allow_all=True), run_store=runs, crash_injector=crash_hook)

            return GraphWorkflow(scope, gateway_factory=lambda ledger: ModelGateway(backend, ledger),
                knowledge_factory=lambda ledger: KnowledgeService(scope, EmbeddingGateway(FixtureEmbeddingProvider(64), ledger)),
                agent_factory=agent_factory, trusted_configuration={"demo": "write-reconciliation-v1"},
                policy_id="artifact-v1", verifier=FileStateVerifier(target, content_equals="ready"))

        first = build(request_write=True, crash_hook=crash)
        workflow_id = first.workflow_id("demo-thread", "demo-turn")
        try:
            first.run("Create artifact.md", intent="code", thread_id="demo-thread", turn_id="demo-turn", mode="lexical")
        except InjectedProcessCrash:
            pass
        else:
            raise AssertionError("expected crash did not occur")
        assert writes == [1] and target.read_text(encoding="utf-8") == "ready"
        child_id = runs.list_runs()[0]["run_id"]
        reopened = build(request_write=False)
        result = reopened.resume(workflow_id)
        assert result.child_run_id == child_id and result.code_run_status == "verified" and writes == [1]
        again = reopened.resume(workflow_id)
        assert again.budget == result.budget and len(runs.list_runs()) == 1
        print(json.dumps({"mode": "offline_fixture", "workflow_id": workflow_id, "child_run_id": child_id,
                          "code_run_status": result.code_run_status, "physical_writes": len(writes),
                          "terminal_resume_requests_added": again.budget["requests"] - result.budget["requests"],
                          "budget": result.budget}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
