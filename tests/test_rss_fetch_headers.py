from urllib.error import HTTPError

from src.ingestion.rss import FeedConfig, _fetch_feed_document, _rss_user_agents


class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._payload


def test_fetch_feed_document_rotates_user_agents(monkeypatch):
    seen_user_agents: list[str] = []

    def fake_urlopen(request, timeout):
        seen_user_agents.append(request.headers["User-agent"])
        if len(seen_user_agents) == 1:
            raise HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)
        return _Response(b"<rss><channel></channel></rss>")

    monkeypatch.delenv("RSS_FEED_USER_AGENTS", raising=False)
    monkeypatch.setattr("src.ingestion.rss.urlopen", fake_urlopen)

    payload = _fetch_feed_document(FeedConfig(name="Example", url="https://example.com/feed", retries=0))

    assert payload == b"<rss><channel></channel></rss>"
    assert len(seen_user_agents) == 2
    assert seen_user_agents[0] != seen_user_agents[1]


def test_rss_user_agents_uses_env_delimiter(monkeypatch):
    monkeypatch.setenv("RSS_FEED_USER_AGENTS", "Agent One||Agent Two")

    assert _rss_user_agents() == ("Agent One", "Agent Two")
