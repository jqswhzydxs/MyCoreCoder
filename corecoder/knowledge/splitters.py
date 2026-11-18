"""AST and Markdown sections split into exact, contiguous source line spans."""

from __future__ import annotations

import ast
import bisect
import re
from itertools import pairwise

from .models import Chunk, ProjectScope, SourceDocument, SplitterConfig, digest


def _python_sections(document: SourceDocument) -> tuple[list[tuple[int, int, str, tuple]], bool]:
    lines = document.text.splitlines(keepends=True)
    try:
        tree = ast.parse(document.text)
    except (SyntaxError, ValueError, RecursionError):
        return [(1, len(lines), "", ())], True
    symbols = []

    def visit(node, parents: tuple[str, ...] = ()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parents = (*parents, node.name)
            start = min([node.lineno, *[decorator.lineno for decorator in node.decorator_list]])
            symbols.append((start, node.end_lineno, ".".join(parents)))
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree)
    boundaries = sorted({1, len(lines) + 1, *[start for start, _, _ in symbols], *[end + 1 for _, end, _ in symbols]})
    sections = []
    for start, next_start in pairwise(boundaries):
        owners = [symbol for begin, end, symbol in symbols if begin <= start <= end]
        owner = max(owners, key=lambda s: (s.count("."), len(s))) if owners else ""
        if sections and sections[-1][2] == owner and sections[-1][1] + 1 == start:
            old = sections.pop()
            sections.append((old[0], next_start - 1, owner, ()))
        else:
            sections.append((start, next_start - 1, owner, ()))
    return sections, False


def _markdown_sections(document: SourceDocument) -> list[tuple[int, int, str, tuple]]:
    lines = document.text.splitlines(keepends=True)
    starts = [(1, ())]
    headings: list[tuple[int, str]] = []
    fence = ""
    for index, line in enumerate(lines, 1):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match[1]
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not line[match.end():].strip():
                fence = ""
            continue
        if fence:
            continue
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        start = index
        if heading:
            level, title = len(heading[1]), heading[2]
        elif (index > 1 and re.match(r"^ {0,3}(=+|-+)\s*$", line) and lines[index - 2].strip()
              and not re.match(r"^ {0,3}(#{1,6}\s|`{3,}|~{3,})", lines[index - 2])):
            level, title, start = (1 if line.lstrip().startswith("=") else 2), lines[index - 2].strip(), index - 1
        else:
            continue
        headings = [(l, t) for l, t in headings if l < level] + [(level, title)]
        if starts[-1][0] == start:
            starts[-1] = (start, tuple(t for _, t in headings))
        else:
            starts.append((start, tuple(t for _, t in headings)))
    return [(start, (starts[i + 1][0] - 1) if i + 1 < len(starts) else len(lines), "", heading)
            for i, (start, heading) in enumerate(starts)]


def split_document(scope: ProjectScope, document: SourceDocument, config: SplitterConfig | None = None) -> list[Chunk]:
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    config = config or SplitterConfig()
    lines = document.text.splitlines(keepends=True)
    if not lines or not document.text.strip():
        return []
    sections, fallback = _python_sections(document) if document.language == "python" else (_markdown_sections(document), False)
    splitter = RecursiveCharacterTextSplitter(chunk_size=config.chunk_size, chunk_overlap=config.chunk_overlap,
                                              length_function=len, add_start_index=True, strip_whitespace=False,
                                              separators=["\n\n", "\n", " ", ""])
    chunks = {}
    for start, end, symbol, heading in sections:
        text = "".join(lines[start - 1:end])
        if not text.strip():
            continue
        offsets = [0]
        for line in lines[start - 1:end]:
            offsets.append(offsets[-1] + len(line))
        # LangChain owns the recursive character splitting; expand each split
        # to complete lines so metadata never claims a fabricated source span.
        documents = splitter.split_documents([Document(page_content=text, metadata={"source_path": document.source_path})])
        for piece in documents:
            offset = piece.metadata["start_index"]
            first = max(0, bisect.bisect_right(offsets, offset) - 1)
            last = min(len(offsets) - 2, bisect.bisect_left(offsets, offset + len(piece.page_content)) - 1)
            line_start, line_end = start + first, start + last
            body = "".join(lines[line_start - 1:line_end])
            if not body.strip():
                continue
            chunk_id = digest([scope.project_id, document.source_path, document.raw_hash, line_start, line_end, config.version])
            chunks[chunk_id] = Chunk(chunk_id, scope.project_id, document.source_path, line_start, line_end, body,
                                     document.raw_hash, document.normalized_hash, config.version, document.language,
                                     document.source_kind, symbol, heading, fallback)
    return sorted(chunks.values(), key=lambda c: (c.start_line, c.end_line, c.chunk_id))
