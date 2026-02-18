"""Unit tests for src/ingestion/newsblur."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.ingestion.newsblur import (
    NewsBlurClient,
    NewsBlurConfig,
    _story_to_rss_record,
    fetch_newsblur_records,
    load_newsblur_config_from_env,
)


# ---------------------------------------------------------------------------
# load_newsblur_config_from_env
# ---------------------------------------------------------------------------


def test_load_newsblur_config_returns_none_when_no_credentials(monkeypatch):
    monkeypatch.delenv("NEWSBLUR_USERNAME", raising=False)
    monkeypatch.delenv("NEWSBLUR_PASSWORD", raising=False)
    assert load_newsblur_config_from_env() is None


def test_load_newsblur_config_returns_none_when_only_username(monkeypatch):
    monkeypatch.setenv("NEWSBLUR_USERNAME", "alice")
    monkeypatch.delenv("NEWSBLUR_PASSWORD", raising=False)
    assert load_newsblur_config_from_env() is None


def test_load_newsblur_config_returns_config_when_both_set(monkeypatch):
    monkeypatch.setenv("NEWSBLUR_USERNAME", "alice")
    monkeypatch.setenv("NEWSBLUR_PASSWORD", "secret")
    monkeypatch.setenv("NEWSBLUR_LATEST_LIMIT", "25")
    monkeypatch.setenv("NEWSBLUR_FETCH_ORIGINAL_TEXT", "true")

    config = load_newsblur_config_from_env()
    assert config is not None
    assert config.username == "alice"
    assert config.password == "secret"
    assert config.latest_limit == 25
    assert config.fetch_original_text is True


def test_load_newsblur_config_defaults(monkeypatch):
    monkeypatch.setenv("NEWSBLUR_USERNAME", "bob")
    monkeypatch.setenv("NEWSBLUR_PASSWORD", "pass")
    for key in (
        "NEWSBLUR_BASE_URL",
        "NEWSBLUR_TIMEOUT_S",
        "NEWSBLUR_RETRIES",
        "NEWSBLUR_BACKOFF_BASE_S",
        "NEWSBLUR_LATEST_LIMIT",
        "NEWSBLUR_FETCH_ORIGINAL_TEXT",
    ):
        monkeypatch.delenv(key, raising=False)

    config = load_newsblur_config_from_env()
    assert config is not None
    assert config.base_url == "https://www.newsblur.com"
    assert config.timeout_s == 15.0
    assert config.retries == 2
    assert config.backoff_base_s == 1.0
    assert config.latest_limit == 50
    assert config.fetch_original_text is True


# ---------------------------------------------------------------------------
# _story_to_rss_record
# ---------------------------------------------------------------------------


def _make_story(**overrides) -> dict:
    base: dict = {
        "story_title": "Test Story",
        "story_permalink": "https://example.com/story",
        "story_feed_id": 42,
        "id": "story-guid-abc",
        "story_hash": "42:abc123",
        "story_timestamp": 1_700_000_000,
        "story_content": "<p>Full article content here.</p>",
        "story_summary": "Summary only.",
    }
    base.update(overrides)
    return base


def test_story_to_rss_record_basic():
    story = _make_story()
    record = _story_to_rss_record(story)

    assert record.source_type == "rss"
    assert record.title == "Test Story"
    assert record.url == "https://example.com/story"
    assert record.feed_name == "42"
    assert record.guid == "story-guid-abc"
    assert record.source_key == "guid:story-guid-abc"
    assert "Full article content" in record.content
    assert record.published_at is not None
    assert record.published_at.tzinfo is not None


def test_story_to_rss_record_timestamp_to_utc():
    # 1_700_000_000 == 2023-11-14T22:13:20Z
    story = _make_story(story_timestamp=1_700_000_000)
    record = _story_to_rss_record(story)
    assert record.published_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_story_to_rss_record_zero_timestamp_yields_no_date():
    # A story_timestamp of 0 is treated as "no date" rather than Unix epoch.
    story = _make_story(story_timestamp=0, story_date=None)
    record = _story_to_rss_record(story)
    assert record.published_at is None


def test_story_to_rss_record_falls_back_to_story_summary_when_no_content():
    story = _make_story(story_content="")
    record = _story_to_rss_record(story)
    assert record.content == "Summary only."


def test_story_to_rss_record_calls_fetch_full_text_when_no_content():
    story = _make_story(story_content="", story_summary="")

    def fake_fetch(story_hash: str) -> str:
        return f"Full text for {story_hash}"

    record = _story_to_rss_record(story, fetch_full_text=fake_fetch)
    assert record.content == "Full text for 42:abc123"


def test_story_to_rss_record_does_not_call_fetch_full_text_when_content_present():
    story = _make_story(story_content="Already got content.")
    calls: list[str] = []

    def fake_fetch(story_hash: str) -> str:
        calls.append(story_hash)
        return "should not be called"

    record = _story_to_rss_record(story, fetch_full_text=fake_fetch)
    assert calls == []
    assert record.content == "Already got content."


def test_story_to_rss_record_url_fallback_source_key():
    story = _make_story(id=None, story_hash=None)
    record = _story_to_rss_record(story)
    assert record.source_key.startswith("url:")
    assert record.guid is None


def test_story_to_rss_record_hash_fallback_when_no_guid_or_url():
    story = _make_story(id=None, story_hash=None, story_permalink="")
    record = _story_to_rss_record(story)
    assert record.source_key.startswith("hash:")


# ---------------------------------------------------------------------------
# fetch_newsblur_records
# ---------------------------------------------------------------------------


def _fake_response(payload: object) -> MagicMock:
    """Build a mock context-manager response that returns JSON bytes."""
    body = json.dumps(payload).encode()
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read = MagicMock(return_value=body)
    return mock_resp


def _patch_opener(responses: list) -> MagicMock:
    """Return a mock opener whose .open() yields successive _fake_response values."""
    opener = MagicMock()
    opener.open.side_effect = responses
    return opener


def test_fetch_newsblur_records_returns_empty_when_no_feeds():
    config = NewsBlurConfig(username="alice", password="secret")

    login_resp = _fake_response({"result": "ok"})
    feeds_resp = _fake_response({"feeds": {}})

    with patch("src.ingestion.newsblur.build_opener") as mock_build:
        mock_opener = MagicMock()
        mock_opener.open.side_effect = [login_resp, feeds_resp]
        mock_build.return_value = mock_opener

        records, error = fetch_newsblur_records(config)

    assert records == []
    assert error is None


def test_fetch_newsblur_records_converts_stories_to_rss_records():
    config = NewsBlurConfig(username="alice", password="secret", latest_limit=10)

    story = {
        "story_title": "Gegenpressing Tactics",
        "story_permalink": "https://example.com/gegenpressing",
        "story_feed_id": 1,
        "id": "guid-001",
        "story_hash": "1:abc",
        "story_timestamp": 1_700_100_000,
        "story_content": "<p>Full analysis.</p>",
        "story_summary": "Short summary.",
    }

    login_resp = _fake_response({"result": "ok"})
    feeds_resp = _fake_response({"feeds": {"1": {"id": 1, "feed_title": "Example"}}})
    river_resp = _fake_response({"stories": [story]})

    with patch("src.ingestion.newsblur.build_opener") as mock_build:
        mock_opener = MagicMock()
        mock_opener.open.side_effect = [login_resp, feeds_resp, river_resp]
        mock_build.return_value = mock_opener

        records, error = fetch_newsblur_records(config)

    assert error is None
    assert len(records) == 1
    assert records[0].title == "Gegenpressing Tactics"
    assert records[0].source_key == "guid:guid-001"
    assert "Full analysis" in records[0].content


def test_fetch_newsblur_records_returns_error_on_network_failure():
    config = NewsBlurConfig(username="alice", password="secret", retries=0)

    with patch("src.ingestion.newsblur.build_opener") as mock_build:
        mock_opener = MagicMock()
        mock_opener.open.side_effect = OSError("connection refused")
        mock_build.return_value = mock_opener

        records, error = fetch_newsblur_records(config)

    assert records == []
    assert isinstance(error, OSError)


def test_fetch_newsblur_records_respects_latest_limit():
    config = NewsBlurConfig(username="alice", password="secret", latest_limit=1)

    stories = [
        {
            "story_title": f"Story {i}",
            "story_permalink": f"https://example.com/story-{i}",
            "story_feed_id": 1,
            "id": f"guid-{i:03d}",
            "story_hash": f"1:{i:03d}",
            "story_timestamp": 1_700_000_000 + i,
            "story_content": f"Content {i}",
            "story_summary": "",
        }
        for i in range(5)
    ]

    login_resp = _fake_response({"result": "ok"})
    feeds_resp = _fake_response({"feeds": {"1": {}}})
    river_resp = _fake_response({"stories": stories})

    with patch("src.ingestion.newsblur.build_opener") as mock_build:
        mock_opener = MagicMock()
        mock_opener.open.side_effect = [login_resp, feeds_resp, river_resp]
        mock_build.return_value = mock_opener

        records, error = fetch_newsblur_records(config)

    assert error is None
    assert len(records) == 1  # capped at latest_limit=1


# ---------------------------------------------------------------------------
# NewsBlurClient.fetch_original_text_for_story
# ---------------------------------------------------------------------------


def test_fetch_original_text_returns_empty_string_on_failure():
    config = NewsBlurConfig(username="alice", password="secret", retries=0)
    client = NewsBlurClient(config)

    with patch.object(client, "_request_with_retry", side_effect=OSError("timeout")):
        result = client.fetch_original_text_for_story("1:abc")

    assert result == ""
