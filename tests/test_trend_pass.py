from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.generation import trend_pass


def test_parse_trend_candidates_requires_two_or_more() -> None:
    payload = json.dumps(
        [
            {
                "rank": 1,
                "topic": "High-wing overloads creating weak-side underlap opportunities in final-third rotations",
                "justification": "Mentions accelerated across tactical newsletters and post-match video analysis.",
                "source_count": 4,
            }
        ]
    )
    assert trend_pass._parse_trend_candidates(payload) == []


def test_validate_topic_rules() -> None:
    assert trend_pass._validate_topic("Compact back-three rest-defense against transition-heavy 4-2-4 pressing traps")
    assert not trend_pass._validate_topic("football analysis")
    assert not trend_pass._validate_topic("too short")


def test_is_exact_duplicate_normalises_text() -> None:
    assert trend_pass._is_exact_duplicate(
        "Final: Pressing Triggers in Possession-Restart Sequences!",
        ["Final Pressing triggers in possession restart sequences"],
    )


def test_is_duplicate_short_circuits_on_exact_match(monkeypatch) -> None:
    semantic_called = False

    def fake_semantic(*_args, **_kwargs):
        nonlocal semantic_called
        semantic_called = True
        return False, 0.0

    monkeypatch.setattr(trend_pass, "_is_semantic_duplicate", fake_semantic)
    duplicate, score = trend_pass._is_duplicate("Topic A", ["Topic A"], settings=SimpleNamespace())

    assert duplicate is True
    assert score is None
    assert semantic_called is False


def test_relative_age_formats_days() -> None:
    now = datetime(2026, 2, 18, 12, 0, 0)
    assert trend_pass._relative_age(now, now) == "today"
    assert trend_pass._relative_age(now - timedelta(days=1), now) == "1 day ago"
    assert trend_pass._relative_age(now - timedelta(days=5), now) == "5 days ago"


def test_source_type_label() -> None:
    assert trend_pass._source_type_label("youtube") == "[TRANSCRIPT]"
    assert trend_pass._source_type_label("rss") == "[ARTICLE]"


def test_build_sources_summary_uses_long_then_short_snippets() -> None:
    now = datetime(2026, 2, 18, 12, 0, 0)
    rows = []
    for idx in range(11):
        rows.append(
            (
                f"Title {idx}",
                "x" * 1000,
                "youtube" if idx % 2 else "rss",
                now - timedelta(days=idx),
                None,
            )
        )

    summary = trend_pass._build_sources_summary(rows, now)
    lines = summary.splitlines()

    assert len(lines) == 11
    assert lines[0].startswith("- [ARTICLE | today] Title 0")
    assert "[TRANSCRIPT" in lines[1]
    assert lines[9].count("x") == trend_pass._LONG_SNIPPET_CHARS
    assert lines[10].count("x") == trend_pass._CONTENT_SNIPPET_CHARS


def test_build_source_activity_summary_formats_buckets() -> None:
    now = datetime(2026, 2, 18, 12, 0, 0)
    rows = [
        ("a1", "", "rss", now - timedelta(days=0), None),
        ("a2", "", "rss", now - timedelta(days=1), None),
        ("t1", "", "youtube", now - timedelta(days=1), None),
        ("a3", "", "rss", now - timedelta(days=4), None),
        ("t2", "", "youtube", now - timedelta(days=5), None),
    ]

    summary = trend_pass._build_source_activity_summary(rows, now, 7)

    assert "last 2 days — articles: 2, transcripts: 1" in summary
    assert "3–7 days ago — articles: 1, transcripts: 1" in summary


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text

    def create(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


class _FakeAnthropic:
    def __init__(self, api_key: str, *, text: str = "") -> None:
        self.api_key = api_key
        self.messages = _FakeMessages(text)


def test_run_trend_pass_requeries_with_expanded_lookback(monkeypatch) -> None:
    calls: list[int] = []

    def fake_query_sources(_connection, lookback_days: int):
        calls.append(lookback_days)
        if lookback_days == 7:
            return [("short", "text", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 5
        return [("expanded", "text", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 15

    response_payload = json.dumps(
        [
            {
                "rank": 1,
                "topic": "Narrow fullback inversion supporting central overloads against hybrid man-oriented pressing blocks",
                "justification": "Recent mentions increased in both podcasts and tactical writeups.",
                "source_count": 5,
            },
            {
                "rank": 2,
                "topic": "Delayed striker decoy runs opening half-space cutback lanes for opposite-side wingers",
                "justification": "Appears in match breakdown videos and analyst columns this week.",
                "source_count": 3,
            },
        ]
    )

    monkeypatch.setattr(trend_pass, "_query_sources", fake_query_sources)
    monkeypatch.setattr(trend_pass, "_fetch_recent_report_topics", lambda *_a, **_k: [])
    monkeypatch.setattr(trend_pass, "_is_duplicate", lambda *_a, **_k: (False, 0.31))
    monkeypatch.setattr(trend_pass, "Anthropic", lambda api_key: _FakeAnthropic(api_key, text=response_payload))

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    result = trend_pass.run_trend_pass(connection=object(), settings=settings, lookback_days=7)

    assert calls == [7, 14]
    assert result.topic.startswith("Narrow fullback inversion")
    assert result.lookback_days == 14
    assert result.dedup_max_similarity == pytest.approx(0.31)


def test_run_trend_pass_returns_fallback_when_no_rows(monkeypatch) -> None:
    monkeypatch.setattr(trend_pass, "_query_sources", lambda *_args, **_kwargs: [])

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    result = trend_pass.run_trend_pass(connection=object(), settings=settings, lookback_days=7)

    assert result.topic == trend_pass._FALLBACK_TOPIC
    assert result.lookback_days == 14
    assert result.dedup_max_similarity is None


def test_run_trend_pass_raises_when_no_valid_candidates(monkeypatch) -> None:
    rows = [("t", "content", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 15
    monkeypatch.setattr(trend_pass, "_query_sources", lambda *_a, **_k: rows)
    monkeypatch.setattr(trend_pass, "_fetch_recent_report_topics", lambda *_a, **_k: [])
    monkeypatch.setattr(trend_pass, "Anthropic", lambda api_key: _FakeAnthropic(api_key, text="not json"))

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    with pytest.raises(trend_pass.TrendPassError):
        trend_pass.run_trend_pass(connection=object(), settings=settings)
