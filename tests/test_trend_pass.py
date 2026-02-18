from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from src.generation import trend_pass


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
    # Top 10 rows get long snippets.
    assert lines[9].count("x") == trend_pass._LONG_SNIPPET_CHARS
    # Row 11 gets short snippet.
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
    def create(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="adaptive lookback topic")])


class _FakeAnthropic:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.messages = _FakeMessages()


def test_run_trend_pass_requeries_with_expanded_lookback(monkeypatch) -> None:
    calls: list[int] = []

    def fake_query_sources(_connection, lookback_days: int):
        calls.append(lookback_days)
        if lookback_days == 7:
            return [("short", "text", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 5
        return [("expanded", "text", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 15

    monkeypatch.setattr(trend_pass, "_query_sources", fake_query_sources)
    monkeypatch.setattr(trend_pass, "Anthropic", _FakeAnthropic)

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    result = trend_pass.run_trend_pass(connection=object(), settings=settings, lookback_days=7)

    assert calls == [7, 14]
    assert result.topic == "adaptive lookback topic"
    assert result.lookback_days == 14


def test_run_trend_pass_returns_fallback_when_no_rows(monkeypatch) -> None:
    monkeypatch.setattr(trend_pass, "_query_sources", lambda *_args, **_kwargs: [])

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    result = trend_pass.run_trend_pass(connection=object(), settings=settings, lookback_days=7)

    assert result.topic == trend_pass._FALLBACK_TOPIC
    assert result.lookback_days == 14
    assert result.dedup_max_similarity is None
