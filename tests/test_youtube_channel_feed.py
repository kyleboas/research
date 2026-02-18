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


def test_fetch_channel_latest_videos_uses_youtube_rss(monkeypatch) -> None:
    payload = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom' xmlns:yt='http://www.youtube.com/xml/schemas/2015'>
  <entry>
    <yt:videoId>abc123xyz00</yt:videoId>
    <title>Match Analysis</title>
    <link rel='alternate' href='https://www.youtube.com/watch?v=abc123xyz00'/>
    <published>2026-01-01T12:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>def456uvw99</yt:videoId>
    <title>Press Conference</title>
    <link rel='alternate' href='https://www.youtube.com/watch?v=def456uvw99'/>
    <published>2026-01-02T12:00:00+00:00</published>
  </entry>
</feed>
"""

    def _fake_urlopen(request, timeout):
        del timeout
        assert request.full_url == "https://www.youtube.com/feeds/videos.xml?channel_id=UC123"
        return _FakeResponse(payload)

    monkeypatch.setattr("src.ingestion.youtube.urlopen", _fake_urlopen)

    records = fetch_channel_latest_videos(
        YouTubeChannelConfig(name="Test", channel_id="UC123", latest_limit=1),
        api_key="ignored",
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
