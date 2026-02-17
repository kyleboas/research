from datetime import UTC, datetime

from src.ingestion.rss import RSSRecord
from src.pipeline import _insert_sources


class _Cursor:
    def __init__(self, table):
        self._table = table

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def executemany(self, _query, rows):
        for source_type, source_key, title, published_at, metadata_json in rows:
            key = (source_type, source_key)
            if key in self._table:
                continue
            self._table[key] = {
                "title": title,
                "published_at": published_at,
                "metadata_json": metadata_json,
            }


class _Connection:
    def __init__(self):
        self.rows = {}
        self.commit_count = 0

    def cursor(self):
        return _Cursor(self.rows)

    def commit(self):
        self.commit_count += 1


def test_insert_sources_is_idempotent_for_same_records() -> None:
    record = RSSRecord(
        source_type="rss",
        source_key="guid:abc-123",
        title="A test post",
        url="https://example.com/post",
        published_at=datetime(2025, 1, 1, tzinfo=UTC),
        content="post body",
        feed_name="example",
        guid="abc-123",
    )
    connection = _Connection()

    _insert_sources(connection, [record])
    _insert_sources(connection, [record])

    assert len(connection.rows) == 1
    assert ("rss", "guid:abc-123") in connection.rows
    assert connection.commit_count == 2
