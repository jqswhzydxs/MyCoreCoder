"""Select stable UTF-8 source files without traversing private/generated data."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .models import KnowledgeError, LoadReport, ProjectScope, SourceDocument, text_hash
from .scope import authorized_path, is_reparse

_EXCLUDED = frozenset({".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
                       "node_modules", ".corecoder", ".tmp", "dist", "build", ".idea", ".aws", ".codex", ".agents"})
_PRIVATE_NAMES = frozenset({"credentials.md", "secrets.md", "sessions.md", "trace.md", "credentials.py", "secrets.py"})


def _stable_signature(info) -> tuple:
    # On Windows, path.stat() and fstat() can report different ctime semantics.
    # Compare ctime within each API below, while identity/size/mtime agree across APIs.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def read_source(scope: ProjectScope, source_path: str, *, max_file_bytes: int = 1024 * 1024) -> SourceDocument:
    path = authorized_path(scope, source_path)
    try:
        before = path.stat()
        if before.st_size > max_file_bytes:
            raise KnowledgeError("oversize", "source exceeds the configured file size limit")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(max_file_bytes + 1)
            finished = os.fstat(stream.fileno())
        after_path = authorized_path(scope, source_path)
        after = after_path.stat()
    except OSError:
        raise KnowledgeError("unreadable", "source cannot be read") from None
    if len(raw) > max_file_bytes:
        raise KnowledgeError("oversize", "source exceeds the configured file size limit")
    if (path != after_path or len({_stable_signature(info) for info in (before, opened, finished, after)}) != 1
            or before.st_ctime_ns != after.st_ctime_ns or opened.st_ctime_ns != finished.st_ctime_ns):
        raise KnowledgeError("unstable", "source changed while being read")
    if any(byte < 32 and byte not in {9, 10, 12, 13} for byte in raw):
        raise KnowledgeError("binary", "source contains binary data")
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise KnowledgeError("invalid_encoding", "source is not valid UTF-8") from None
    first = source_path.split("/", 1)[0]
    kind = {"tests": "test", "docs": "design_document", "article": "article"}.get(first, "source_code" if path.suffix.lower() == ".py" else "documentation")
    return SourceDocument(source_path, text, hashlib.sha256(raw).hexdigest(), text_hash(text),
                          "python" if path.suffix.lower() == ".py" else "markdown", kind, len(raw))


def _ignore_specs(scope: ProjectScope) -> list[tuple[Path, object]]:
    try:
        from pathspec import GitIgnoreSpec
    except ImportError:
        raise KnowledgeError("configuration_error", 'indexing requires corecoder[knowledge] (pathspec)') from None
    return [(scope.root, GitIgnoreSpec.from_lines([]))]


def load_sources(scope: ProjectScope, *, max_file_bytes: int = 1024 * 1024) -> LoadReport:
    if not 1 <= max_file_bytes <= 16 * 1024 * 1024:
        raise ValueError("file size limit must be between 1 byte and 16 MiB")
    report = LoadReport()
    base_specs = _ignore_specs(scope)
    from pathspec import GitIgnoreSpec
    root_ignore = scope.root / ".gitignore"
    if root_ignore.is_file() and not is_reparse(root_ignore):
        try:
            base_specs.append((scope.root, GitIgnoreSpec.from_lines(root_ignore.read_text(encoding="utf-8").splitlines())))
        except (OSError, UnicodeDecodeError):
            raise KnowledgeError("configuration_error", "project ignore file cannot be decoded safely") from None

    def ignored(path: Path, specs: list, *, directory: bool = False) -> bool:
        result = False
        for base, spec in specs:
            try:
                relative = path.relative_to(base).as_posix() + ("/" if directory else "")
            except ValueError:
                continue
            match = spec.check_file(relative)
            if match.include is not None:
                result = bool(match.include)
        return result

    def add(path: Path, specs: list) -> None:
        if path.suffix.lower() not in {".py", ".md"} or path.name.lower() in _PRIVATE_NAMES or path.name.startswith("."):
            report.skip("excluded")
            return
        if ignored(path, specs):
            report.skip("ignored")
            return
        source_path = path.relative_to(scope.root).as_posix()
        try:
            report.documents.append(read_source(scope, source_path, max_file_bytes=max_file_bytes))
        except KnowledgeError as exc:
            report.skip(exc.category)

    def walk(directory: Path, inherited: list) -> None:
        try:
            if is_reparse(directory):
                report.skip("reparse_point")
                return
            directory.resolve(strict=True).relative_to(scope.root)
            if directory.resolve() == scope.data_root:
                report.skip("excluded")
                return
            specs = list(inherited)
            ignore = directory / ".gitignore"
            if directory != scope.root and ignore.is_file() and not is_reparse(ignore):
                specs.append((directory, GitIgnoreSpec.from_lines(ignore.read_text(encoding="utf-8").splitlines())))
            entries = sorted(directory.iterdir())
        except (OSError, ValueError, UnicodeDecodeError):
            report.skip("unreadable")
            return
        for entry in entries:
            try:
                if is_reparse(entry):
                    report.skip("reparse_point")
                elif entry.is_dir():
                    if entry.name in _EXCLUDED or entry.name.startswith(".") or entry.name.endswith(".egg-info"):
                        report.skip("excluded")
                    elif entry.relative_to(scope.root).as_posix() == "evals/results" or ignored(entry, specs, directory=True):
                        report.skip("ignored")
                    else:
                        walk(entry, specs)
                elif entry.is_file():
                    add(entry, specs)
            except OSError:
                report.skip("unreadable")

    seen = set()
    for relative in scope.directories:
        directory = scope.root / relative
        # Parent directories cannot be used to bypass exclusions/ignore rules.
        if any(part in _EXCLUDED for part in Path(relative).parts) or relative.startswith("evals/results"):
            report.skip("excluded")
            continue
        if not directory.is_dir():
            continue
        specs = list(base_specs)
        current = scope.root
        allowed = True
        for part in Path(relative).parts:
            try:
                if current != scope.root:
                    parent_ignore = current / ".gitignore"
                    if parent_ignore.is_file() and not is_reparse(parent_ignore):
                        specs.append((current, GitIgnoreSpec.from_lines(parent_ignore.read_text(encoding="utf-8").splitlines())))
                current = current / part
                if is_reparse(current) or current.resolve() == scope.data_root:
                    report.skip("reparse_point" if is_reparse(current) else "excluded")
                    allowed = False
                    break
                if ignored(current, specs, directory=True):
                    report.skip("ignored")
                    allowed = False
                    break
            except (OSError, UnicodeDecodeError):
                report.skip("unreadable")
                allowed = False
                break
        if allowed:
            walk(directory, specs)
    for readme in sorted(scope.root.glob("README*.md")):
        if readme.is_file():
            add(readme, base_specs)
    unique = []
    for document in sorted(report.documents, key=lambda doc: doc.source_path):
        if document.source_path not in seen:
            unique.append(document)
            seen.add(document.source_path)
    report.documents = unique
    return report
