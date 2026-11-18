"""Bind local identities and authorize source paths before filesystem access."""

from __future__ import annotations

import getpass
import os
import socket
import stat
from pathlib import Path

from .models import KnowledgeError, ProjectScope, digest, relative_path


def create_scope(root: str | Path, *, data_root: str | Path | None = None,
                 directories: tuple[str, ...] = ("corecoder", "docs", "tests"), include_article: bool = False) -> ProjectScope:
    try:
        canonical = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise KnowledgeError("configuration_error", "project root is not accessible") from None
    if not canonical.is_dir():
        raise KnowledgeError("configuration_error", "project root must be a directory")
    if include_article and "article" not in directories:
        directories = (*directories, "article")
    storage = Path(data_root).expanduser() if data_root else canonical / ".corecoder/knowledge"
    if not storage.is_absolute():
        storage = canonical / storage
    storage = storage.resolve()
    # A local installation identifier, not a server authentication mechanism.
    # SDK stdio transports intentionally inherit a small environment which
    # can omit COMPUTERNAME. The OS hostname stays stable across that boundary.
    owner = digest(["local-owner-v1", socket.gethostname(), getpass.getuser()])
    return ProjectScope(owner, digest(["project-root-v1", os.path.normcase(str(canonical))]), canonical, storage, directories)


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def authorized_path(scope: ProjectScope, source_path: str) -> Path:
    try:
        relative_path(source_path)
        candidate = scope.root / source_path
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(scope.root)
        current = candidate
        while current != scope.root:
            if is_reparse(current):
                raise ValueError("reparse point")
            current = current.parent
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise KnowledgeError("source_unavailable", "source is absent or outside the authorized root") from None
