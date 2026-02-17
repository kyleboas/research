from src.processing.retrieval import hybrid_search


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params):
        self.executed.append((query, params))

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self._cursor = _Cursor(rows)

    def cursor(self):
        return self._cursor


def test_hybrid_search_returns_keyword_and_semantic_hits_in_top_k() -> None:
    # Row 1 is mostly text-ranked, row 2 is mostly vector-ranked.
    rows = [
        (101, 1, 0.55, 1, None, 0, "keyword-heavy chunk", {"kind": "keyword"}, "rss", "s1", "Source 1", {}),
        (202, 2, 0.53, None, 1, 1, "semantic-heavy chunk", {"kind": "semantic"}, "rss", "s2", "Source 2", {}),
    ]
    connection = _Connection(rows)

    results = hybrid_search(
        connection,
        query_text="hybrid retrieval check",
        query_embedding=[0.1, 0.2, 0.3],
        top_k=2,
    )

    assert len(results) == 2
    assert any(result.text_rank is not None for result in results)
    assert any(result.vector_rank is not None for result in results)
    assert {result.chunk_metadata["kind"] for result in results} == {"keyword", "semantic"}
