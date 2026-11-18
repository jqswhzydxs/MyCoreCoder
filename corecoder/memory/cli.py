"""Deterministic memory commands. No Chat client or credentials are required."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from ..knowledge.models import KnowledgeError
from ..knowledge.scope import create_scope, is_reparse
from .models import MemoryError
from .service import MemoryService
from .store import MemoryStore


def service_for_root(root=None, data_root=None) -> MemoryService:
    root = Path(root or os.getenv("CORECODER_MEMORY_ROOT") or os.getenv("CORECODER_KNOWLEDGE_ROOT") or Path.cwd()).resolve()
    project_file = root / "corecoder.toml"
    config = {}
    if project_file.exists():
        try:
            if is_reparse(project_file) or project_file.stat().st_size > 64 * 1024:
                raise ValueError("invalid config")
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            config = tomllib.loads(project_file.read_text(encoding="utf-8")).get("memory", {})
            if not isinstance(config, dict):
                raise TypeError("invalid config")
        except (OSError, ValueError, TypeError):
            raise MemoryError("configuration_error", "memory project configuration is invalid") from None
    directory = Path(data_root or os.getenv("CORECODER_MEMORY_DATA_ROOT") or config.get("persist_directory", ".corecoder/memory"))
    if not directory.is_absolute():
        directory = root / directory
    return MemoryService(create_scope(root), MemoryStore(directory / "memories.db"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corecoder memory", description="Manage scoped local preferences without a model")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("remember", "list", "show", "forget", "clear"):
        command = commands.add_parser(name)
        command.add_argument("--root")
        command.add_argument("--data-root")
        command.add_argument("--json", action="store_true")
        if name == "remember":
            command.add_argument("content")
            command.add_argument("--key", default="response.preference")
            command.add_argument("--operation-id")
            command.add_argument("--ttl-seconds", type=float)
        if name in {"show", "forget"}:
            command.add_argument("memory_id")
        if name == "clear":
            command.add_argument("--project-id", required=True, help="Exact project ID from memory list; clears only that project")
    args = parser.parse_args(argv)
    try:
        service = service_for_root(args.root, args.data_root)
        if args.command == "remember":
            result = service.remember(args.content, key=args.key, operation_id=args.operation_id or uuid4().hex,
                                      ttl_seconds=args.ttl_seconds).to_dict()
        elif args.command == "list":
            result = {"scope": service.scope.identity(), "memories": [record.to_dict() for record in service.list()]}
        elif args.command == "show":
            result = service.show(args.memory_id).to_dict()
        elif args.command == "forget":
            result = service.forget(args.memory_id).to_dict()
        else:
            if args.project_id != service.scope.project_id:
                raise MemoryError("scope_mismatch", "clear requires this project's exact identity")
            ids = [record.memory_id for record in service.list() if record.status != "deleted"]
            for memory_id in ids:
                service.forget(memory_id)
            result = {"scope": service.scope.identity(), "deleted": len(ids)}
        # Local preferences are intentionally visible to their local owner.
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif "memories" in result:
            print("project_id=" + service.scope.project_id)
            for record in result["memories"]:
                print(f"{record['memory_id']} {record['status']} {record['key']} {record['content']}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MemoryError, KnowledgeError) as exc:
        error = {"error": {"category": exc.category, "message": str(exc)}}
        print(json.dumps(error, ensure_ascii=False), file=sys.stdout if args.json else sys.stderr)
        return 2 if exc.category in {"invalid_request", "configuration_error"} else 8
    except (OSError, TypeError, ValueError):
        error = {"error": {"category": "configuration_error", "message": "memory configuration or request is invalid"}}
        print(json.dumps(error), file=sys.stdout if args.json else sys.stderr)
        return 2
