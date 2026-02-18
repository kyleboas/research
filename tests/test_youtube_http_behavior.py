from io import BytesIO
from urllib.error import HTTPError

from src.ingestion.youtube import _http_json, normalize_channel


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_http_json_retries_on_429(monkeypatch) -> None:
    calls = {"count": 0}

    def _fake_urlopen(request, timeout):
        del request, timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise HTTPError(
                url="https://transcriptapi.com/api/v2/youtube/transcript",
                code=429,
                msg="Too Many Requests",
                hdrs=None,
                fp=BytesIO(b'{"error":"rate limited"}'),
            )
        return _FakeResponse(b'{"transcript":"ok"}')

    monkeypatch.setattr("src.ingestion.youtube.urlopen", _fake_urlopen)
    monkeypatch.setattr("src.ingestion.youtube.time.sleep", lambda *_args, **_kwargs: None)

    payload = _http_json(
        base_url="https://transcriptapi.com/api/v2",
        path="/youtube/transcript",
        query={"video_url": "https://www.youtube.com/watch?v=abc123xyz00"},
        api_key="api-key",
        timeout_s=5,
        retries=2,
    )

    assert payload["transcript"] == "ok"
    assert calls["count"] == 2


def test_http_json_includes_http_error_body(monkeypatch) -> None:
    def _fake_urlopen(request, timeout):
        del request, timeout
        raise HTTPError(
            url="https://transcriptapi.com/api/v2/youtube/transcript",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=BytesIO(b'{"error":"blocked by waf"}'),
        )

    monkeypatch.setattr("src.ingestion.youtube.urlopen", _fake_urlopen)

    try:
        _http_json(
            base_url="https://transcriptapi.com/api/v2",
            path="/youtube/transcript",
            query={"video_url": "https://www.youtube.com/watch?v=abc123xyz00"},
            api_key="api-key",
            timeout_s=5,
            retries=0,
        )
    except RuntimeError as exc:
        message = str(exc)
        assert "HTTP 403 Forbidden" in message
        assert "blocked by waf" in message
    else:
        raise AssertionError("expected RuntimeError")


def test_normalize_channel_accepts_ids_handles_and_urls() -> None:
    assert normalize_channel("UCiVg6vRhuyjsWgHkDNOig6A") == "UCiVg6vRhuyjsWgHkDNOig6A"
    assert normalize_channel("@TED") == "@TED"
    assert normalize_channel("https://www.youtube.com/@TED") == "https://www.youtube.com/@TED"
    assert normalize_channel("BeanymanSports") == "@BeanymanSports"
