"""Scoped FTS5 retrieval with safe identifier and Chinese preprocessing."""

from __future__ import annotations

import re


def tokenize(text: str) -> list[str]:
    tokens = []
    for word in re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", text):
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            tokens.extend([word] if len(word) == 1 else [word[i:i + 2] for i in range(len(word) - 1)])
        else:
            tokens.append(word.lower())
            separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word)
            separated = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", separated)
            tokens.extend(part.lower() for part in re.split(r"[_\s]+", separated) if part)
    return list(dict.fromkeys(tokens))


def match_expression(query: str) -> str:
    # Tokens contain only letters/digits/underscores. No user FTS/SQL syntax
    # reaches MATCH; SQL scope and limits remain separately parameterized.
    return " OR ".join(f'"{token}"' for token in tokenize(query)[:64])


class LexicalStore:
    def __init__(self, manifests):
        self.manifests = manifests

    @staticmethod
    def write_snapshot(connection, scope, snapshot_id, chunks) -> None:
        connection.execute("DELETE FROM chunk_fts WHERE owner_id=? AND project_id=? AND snapshot_id=?",
                           (scope.owner_id, scope.project_id, snapshot_id))
        connection.executemany(
            "INSERT INTO chunk_fts(owner_id,project_id,snapshot_id,chunk_id,tokens) VALUES(?,?,?,?,?)",
            [(scope.owner_id, scope.project_id, snapshot_id, chunk.chunk_id,
              " ".join(tokenize(f"{chunk.source_path} {chunk.symbol} {' '.join(chunk.heading)} {chunk.text}"))) for chunk in chunks],
        )

    def search(self, snapshot_id: str, query: str, top_k: int = 20) -> list[str]:
        expression = match_expression(query)
        if not expression:
            return []
        self.manifests.manifest(snapshot_id)
        scope = self.manifests.scope
        with self.manifests.connect() as connection:
            return [row[0] for row in connection.execute(
                "SELECT chunk_id FROM chunk_fts WHERE chunk_fts MATCH ? AND owner_id=? AND project_id=? AND snapshot_id=? "
                "ORDER BY bm25(chunk_fts), chunk_id LIMIT ?",
                (expression, scope.owner_id, scope.project_id, snapshot_id, top_k),
            )]
