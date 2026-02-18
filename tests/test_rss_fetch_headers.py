from urllib.error import HTTPError
from io import BytesIO

from src.ingestion.rss import FeedConfig, _fetch_feed_document, _rss_request_headers, _rss_user_agent


class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._payload


def test_fetch_feed_document_retries_with_consistent_headers(monkeypatch):
    seen_headers: list[dict[str, str]] = []

    def fake_urlopen(request, timeout):
        seen_headers.append(dict(request.headers.items()))
        if len(seen_headers) == 1:
            raise HTTPError(request.full_url, 403, "Forbidden", hdrs={"Server": "cloudflare"}, fp=BytesIO(b"Access denied"))
        return _Response(b"<rss><channel></channel></rss>")

    monkeypatch.setenv("RSS_FEED_USER_AGENT", "TestAgent/1.0")
    monkeypatch.setattr("src.ingestion.rss.urlopen", fake_urlopen)

    payload = _fetch_feed_document(FeedConfig(name="Example", url="https://example.com/feed", retries=1))

    assert payload == b"<rss><channel></channel></rss>"
    assert len(seen_headers) == 2
    assert seen_headers[0] == seen_headers[1]
    assert seen_headers[0]["User-agent"] == "TestAgent/1.0"
    assert seen_headers[0]["Accept-language"] == "en-US,en;q=0.9"


def test_rss_user_agent_uses_single_env_value(monkeypatch):
    monkeypatch.setenv("RSS_FEED_USER_AGENT", "Agent One")
    monkeypatch.setenv("RSS_FEED_USER_AGENTS", "Agent Two||Agent Three")

    assert _rss_user_agent() == "Agent One"


def test_rss_user_agent_uses_first_legacy_env_value(monkeypatch):
    monkeypatch.delenv("RSS_FEED_USER_AGENT", raising=False)
    monkeypatch.setenv("RSS_FEED_USER_AGENTS", "Agent Two||Agent Three")

    assert _rss_user_agent() == "Agent Two"


def test_rss_request_headers_include_browserish_defaults(monkeypatch):
    monkeypatch.setenv("RSS_FEED_USER_AGENT", "Agent One")

    assert _rss_request_headers() == {
        "User-Agent": "Agent One",
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
