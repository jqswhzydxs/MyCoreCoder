"""Authorized source loading, exact spans, model protocol and durable claims."""

import hashlib
import importlib.util
import json
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from corecoder.config import EmbeddingConfig
from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import EmbeddingBatch, FixtureEmbeddingProvider, OpenAIEmbeddingProvider
from corecoder.knowledge.loaders import load_sources, read_source
from corecoder.knowledge.manifest_store import ManifestStore
from corecoder.knowledge.models import EmbeddingSpace, KnowledgeError, SplitterConfig, relative_path
from corecoder.knowledge.scope import authorized_path, create_scope
from corecoder.workflows.budget_ledger import BudgetExceeded, BudgetLedger, WorkflowBudget

pytestmark = pytest.mark.skipif(importlib.util.find_spec("pathspec") is None, reason="optional knowledge loader dependencies are absent")


def project(tmp_path):
    (tmp_path / "corecoder").mkdir(exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    return create_scope(tmp_path)


def test_loader_filters_gitignore_nested_ignores_private_and_generated_data(tmp_path):
    scope = project(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "corecoder/keep.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "corecoder/ignored.py").write_text("secret = 1\n", encoding="utf-8")
    (tmp_path / "docs/.gitignore").write_text("*.md\n!keep.md\n", encoding="utf-8")
    (tmp_path / "docs/keep.md").write_text("# 中文\n", encoding="utf-8")
    (tmp_path / "docs/ignored.md").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "corecoder/.env").write_text("DO_NOT_READ=fixture\n", encoding="utf-8")
    (tmp_path / "corecoder/.corecoder").mkdir()
    (tmp_path / "corecoder/.corecoder/private.md").write_text("private", encoding="utf-8")
    report = load_sources(scope)
    assert [doc.source_path for doc in report.documents] == ["corecoder/keep.py", "docs/keep.md"]
    assert report.skipped["ignored"] == 2


def test_loader_counts_oversize_binary_and_encoding_failures(tmp_path):
    scope = project(tmp_path)
    (tmp_path / "corecoder/large.py").write_bytes(b"x" * 101)
    (tmp_path / "corecoder/binary.py").write_bytes(b"x\x00y")
    (tmp_path / "corecoder/bad.py").write_bytes(b"\xff")
    report = load_sources(scope, max_file_bytes=100)
    assert report.documents == []
    assert report.skipped == {"oversize": 1, "binary": 1, "invalid_encoding": 1}


def test_overlapping_nested_includes_cannot_bypass_parent_ignore(tmp_path):
    scope = project(tmp_path)
    (tmp_path / "corecoder/.gitignore").write_text("private/\n", encoding="utf-8")
    (tmp_path / "corecoder/private/deep").mkdir(parents=True)
    (tmp_path / "corecoder/private/deep/hidden.py").write_text("DO_NOT_INDEX = True\n", encoding="utf-8")
    scope = replace(scope, directories=("corecoder", "corecoder/private/deep"))
    assert load_sources(scope).documents == []


def test_crlf_has_separate_raw_and_normalized_hashes(tmp_path):
    scope = project(tmp_path)
    raw = b"a = 1\r\nb = 2\r\n"
    (tmp_path / "corecoder/a.py").write_bytes(raw)
    document = read_source(scope, "corecoder/a.py")
    assert document.text == "a = 1\nb = 2\n"
    assert document.raw_hash == hashlib.sha256(raw).hexdigest()
    assert document.raw_hash != document.normalized_hash


@pytest.mark.parametrize("path", ["../outside.py", "C:/secret.py", "/tmp/a.py", "corecoder/../../a.py", "corecoder\\a.py", "./a.py"])
def test_path_contract_rejects_ambiguous_or_external_paths(tmp_path, path):
    scope = project(tmp_path)
    with pytest.raises(ValueError):
        relative_path(path)
    with pytest.raises(KnowledgeError):
        authorized_path(scope, path)


def test_linked_source_cannot_escape_scope(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    scope = project(root)
    outside = tmp_path / "outside.py"
    outside.write_text("private = True", encoding="utf-8")
    link = root / "corecoder/link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("the current Windows account cannot create file symlinks")
    assert load_sources(scope).documents == []
    with pytest.raises(KnowledgeError):
        authorized_path(scope, "corecoder/link.py")


def test_read_changed_during_io_is_not_indexed(tmp_path, monkeypatch):
    scope = project(tmp_path)
    path = tmp_path / "corecoder/a.py"
    path.write_text("a = 1\n", encoding="utf-8")
    original = os.fstat
    calls = []

    def changing(fd):
        calls.append(fd)
        if len(calls) == 2:
            path.write_text("a = 100\n", encoding="utf-8")
        return original(fd)

    monkeypatch.setattr(os, "fstat", changing)
    assert load_sources(scope).skipped["unstable"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_windows_junction_cannot_add_external_sources(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    scope = project(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.py").write_text("PRIVATE_EXTERNAL_SOURCE = True\n", encoding="utf-8")
    link = root / "corecoder/linked"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False)
    assert result.returncode == 0
    try:
        loaded = load_sources(scope)
        assert loaded.documents == [] and loaded.skipped["reparse_point"] == 1
        with pytest.raises(KnowledgeError):
            authorized_path(scope, "corecoder/linked/private.py")
    finally:
        # Remove only the junction itself, never recurse into its target.
        os.rmdir(link)


def require_splitters():
    pytest.importorskip("langchain_text_splitters")
    from corecoder.knowledge.splitters import split_document

    return split_document


def test_python_decorators_nested_symbols_and_all_spans_round_trip(tmp_path):
    split = require_splitters()
    scope = project(tmp_path)
    body = "import os\n\n@decorator\nclass Outer:\n    def method(self):\n        def nested():\n            return 2\n        return nested()\n"
    path = tmp_path / "corecoder/a.py"
    path.write_bytes(body.replace("\n", "\r\n").encode())
    doc = read_source(scope, "corecoder/a.py")
    chunks = split(scope, doc)
    assert {c.symbol for c in chunks} >= {"Outer", "Outer.method", "Outer.method.nested"}
    assert any(c.start_line == 3 and "@decorator" in c.text for c in chunks)
    for chunk in chunks:
        assert chunk.text == "".join(doc.text.splitlines(keepends=True)[chunk.start_line - 1:chunk.end_line])
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in split(scope, doc)]


def test_long_and_broken_python_falls_back_without_losing_lines(tmp_path):
    split = require_splitters()
    scope = project(tmp_path)
    body = "def broken(:\n" + "value = '中文'\n" * 200 + "tail = 1"
    (tmp_path / "corecoder/broken.py").write_text(body, encoding="utf-8")
    chunks = split(scope, read_source(scope, "corecoder/broken.py"), SplitterConfig(300, 50))
    assert len(chunks) > 1 and all(chunk.fallback for chunk in chunks)
    covered = {line for c in chunks for line in range(c.start_line, c.end_line + 1)}
    assert covered == set(range(1, len(body.splitlines()) + 1))
    assert "tail = 1" in chunks[-1].text


def test_markdown_fences_do_not_create_headings_and_empty_files_produce_no_chunks(tmp_path):
    split = require_splitters()
    scope = project(tmp_path)
    (tmp_path / "docs/a.md").write_text("# 中文\n```py\n# not a heading\n```\n## 真实章节\n内容", encoding="utf-8")
    chunks = split(scope, read_source(scope, "docs/a.md"))
    assert [c.heading for c in chunks] == [("中文",), ("中文", "真实章节")]
    (tmp_path / "docs/empty.md").write_text("", encoding="utf-8")
    assert split(scope, read_source(scope, "docs/empty.md")) == []


def test_markdown_setext_headings_retain_the_title_and_underline_source_lines(tmp_path):
    split = require_splitters()
    scope = project(tmp_path)
    (tmp_path / "docs/setext.md").write_text("Title\n=====\nIntro\n\nSubtitle\n--------\nDetails\n", encoding="utf-8")
    doc = read_source(scope, "docs/setext.md")
    chunks = split(scope, doc)
    assert [chunk.heading for chunk in chunks] == [("Title",), ("Title", "Subtitle")]
    assert chunks[1].start_line == 5 and chunks[1].text.startswith("Subtitle\n--------\n")


def test_embedding_fixture_needs_no_credentials_and_shares_budget():
    ledger = BudgetLedger(WorkflowBudget(max_requests=2))
    provider = FixtureEmbeddingProvider(8)
    gateway = EmbeddingGateway(provider, ledger)
    assert len(gateway.embed_documents(["RunStore 运行恢复", "some text"])) == 2
    assert len(gateway.embed_query("RunStore")) == 8
    with pytest.raises(BudgetExceeded):
        gateway.embed_query("third request")
    assert provider.document_requests == provider.query_requests == 1
    assert ledger.snapshot()["unknown_usage_requests"] == 2
    assert ledger.snapshot()["cost_usd"] is None


@pytest.mark.parametrize("vector", [[], [math.nan] * 4, [math.inf] * 4, [0.0] * 3, [True] * 4])
def test_bad_embedding_values_are_controlled_failures(vector):
    provider = Mock()
    provider.describe.return_value = EmbeddingSpace("fixture", "invalid-vector", 4)
    # Do not expose a timeout adapter on a generic mock.
    provider.with_timeout = None
    provider.embed_documents.return_value = EmbeddingBatch((tuple(vector),))
    gateway = EmbeddingGateway(provider)
    with pytest.raises(KnowledgeError, match="embedding"):
        gateway.embed_documents(["fixture"])
    assert gateway.ledger.snapshot()["requests"] == 1


@pytest.mark.parametrize("usage_field", ["prompt_tokens", "total_tokens"])
def test_api_adapter_orders_batch_indices_and_never_inherits_chat_credentials(monkeypatch, usage_field):
    monkeypatch.setenv("OPENAI_API_KEY", "CHAT_FIXTURE_DO_NOT_USE")
    monkeypatch.setenv("CORECODER_EMBEDDING_API_KEY", "EMBEDDING_FIXTURE")
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=1, embedding=[0.0, 1.0]), SimpleNamespace(index=0, embedding=[1.0, 0.0])],
        usage=SimpleNamespace(**{usage_field: 8}),
    )
    factory = Mock(return_value=client)
    provider = OpenAIEmbeddingProvider(EmbeddingConfig(model="fixture-model", base_url="https://embedding.invalid"),
                                       dimensions=2, client_factory=factory)
    gateway = EmbeddingGateway(provider)
    result = gateway.embed_documents(["a", "b"])
    assert result == ((1.0, 0.0), (0.0, 1.0))
    assert factory.call_args.kwargs["api_key"] == "EMBEDDING_FIXTURE"
    assert factory.call_args.kwargs["max_retries"] == 0
    assert gateway.records[0].usage_available


def test_embedding_auth_is_loaded_only_at_request_time(monkeypatch):
    monkeypatch.delenv("CORECODER_EMBEDDING_API_KEY", raising=False)
    provider = OpenAIEmbeddingProvider(EmbeddingConfig(model="fixture-model"))
    assert provider.describe().model_revision == "fixture-model"
    gateway = EmbeddingGateway(provider)
    with pytest.raises(KnowledgeError, match="credentials"):
        gateway.embed_query("query")
    assert gateway.ledger.snapshot()["requests"] == 0


def test_embedding_url_does_not_inherit_chat_url_and_endpoints_separate_cache_spaces(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://chat.invalid/v1")
    monkeypatch.setenv("CORECODER_EMBEDDING_API_KEY", "EMBEDDING_FIXTURE")
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])], usage=SimpleNamespace(total_tokens=1))
    factory = Mock(return_value=client)
    provider = OpenAIEmbeddingProvider(EmbeddingConfig(model="same-model"), dimensions=2, client_factory=factory)
    provider.embed_query("query")
    assert factory.call_args.kwargs["base_url"] == "https://api.openai.com/v1"
    first = OpenAIEmbeddingProvider(EmbeddingConfig(model="same-model", base_url="https://embedding-one.invalid/v1"), dimensions=2)
    second = OpenAIEmbeddingProvider(EmbeddingConfig(model="same-model", base_url="https://embedding-two.invalid/v1"), dimensions=2)
    assert len({provider.describe().space_id, first.describe().space_id, second.describe().space_id}) == 3
    assert first.with_timeout(1).describe() == first.describe()


def test_embedding_retry_limit_and_budget_stop():
    class Failure(Exception):
        status_code = 429
        response = SimpleNamespace(headers={"retry-after": "0"})

    provider = Mock()
    provider.describe.return_value = EmbeddingSpace("fixture", "retry", 2)
    provider.with_timeout = None
    provider.embed_documents.side_effect = Failure("PRIVATE_PROVIDER_ERROR")
    ledger = BudgetLedger(WorkflowBudget(max_requests=2))
    gateway = EmbeddingGateway(provider, ledger, sleep=lambda _: None)
    with pytest.raises(BudgetExceeded):
        gateway.embed_documents(["fixture"])
    assert provider.embed_documents.call_count == 2
    assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(gateway.metrics())


def test_manifest_reopen_transaction_rollback_and_schema_guard(tmp_path):
    scope = project(tmp_path)
    store = ManifestStore(scope)
    assert ManifestStore(scope).status() == {"indexed": False, "snapshot_id": None}
    with pytest.raises(RuntimeError), store.connect(write=True) as connection:
        connection.execute("INSERT INTO chunks VALUES('fixture','project','{}')")
        raise RuntimeError("injected")
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(KnowledgeError, match="schema"):
        ManifestStore(scope)


def test_index_writer_claim_is_transactional_and_expired_lease_can_be_replaced(tmp_path):
    scope = project(tmp_path)
    now = [100.0]
    store = ManifestStore(scope, clock=lambda: now[0], lease_seconds=5)
    first = store.acquire_job()
    with pytest.raises(KnowledgeError, match="another index writer"):
        ManifestStore(scope, clock=lambda: now[0]).acquire_job()
    now[0] = 106.0
    second = store.acquire_job()
    assert first != second
    with pytest.raises(KnowledgeError, match="lease was lost"):
        store.renew(first)
    store.finish_job(second)


def test_concurrent_connections_cannot_claim_the_same_project(tmp_path):
    scope = project(tmp_path)
    ManifestStore(scope)

    def claim(_):
        try:
            return ManifestStore(scope).acquire_job()
        except KnowledgeError as exc:
            return exc.category

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(result == "index_busy" for result in results) == 1


def test_index_project_identity_is_bound_to_the_canonical_root(tmp_path):
    scope = project(tmp_path)
    ManifestStore(scope)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(KnowledgeError, match="another canonical root"):
        ManifestStore(replace(scope, root=other))
