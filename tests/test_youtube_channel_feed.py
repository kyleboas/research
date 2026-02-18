from src.ingestion.youtube import YouTubeChannelConfig, fetch_channel_latest_videos


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_fetch_channel_latest_videos_uses_transcript_provider_api(monkeypatch) -> None:
    payload = b'{"videos":[{"id":"abc123xyz00","title":"Match Analysis","url":"https://www.youtube.com/watch?v=abc123xyz00","publishedAt":"2026-01-01T12:00:00+00:00"}]}'

    def _fake_urlopen(request, timeout):
        del timeout
        assert request.full_url == (
            "https://transcriptapi.com/api/v2/youtube/channel/latest"
            "?channel=UC123&limit=1"
        )
        assert request.headers["Authorization"] == "Bearer api-key"
        return _FakeResponse(payload)

    monkeypatch.setattr("src.ingestion.youtube.urlopen", _fake_urlopen)

    records = fetch_channel_latest_videos(
        YouTubeChannelConfig(name="Test", channel_id="UC123", latest_limit=1),
        api_key="api-key",
        provider_base_url="https://transcriptapi.com/api/v2",
    )

    assert records == [
        {
            "video_id": "abc123xyz00",
            "title": "Match Analysis",
            "url": "https://www.youtube.com/watch?v=abc123xyz00",
            "published_at": "2026-01-01T12:00:00+00:00",
        }
    ]




def test_fetch_channel_latest_videos_raises_when_provider_fails(monkeypatch) -> None:
    from urllib.error import URLError

    def _fake_urlopen(request, timeout):
        del request, timeout
        raise URLError("provider down")

    monkeypatch.setattr("src.ingestion.youtube.urlopen", _fake_urlopen)

    try:
        fetch_channel_latest_videos(
            YouTubeChannelConfig(name="Test", channel_id="UC123", latest_limit=1),
            api_key="api-key",
            provider_base_url="https://transcriptapi.com/api/v2",
        )
    except RuntimeError as exc:
        assert "request failed" in str(exc)
    else:
        raise AssertionError("expected provider failure to be raised")
