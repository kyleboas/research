from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src import pipeline
from src.generation import trend_pass


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text

    def create(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


class _FakeAnthropic:
    def __init__(self, api_key: str, *, text: str = "") -> None:
        self.api_key = api_key
        self.messages = _FakeMessages(text)


def _valid_candidates_payload() -> str:
    return json.dumps(
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
            {
                "rank": 3,
                "topic": "Back-post isolation patterns exploiting narrow fullback recovery angles in transition defense",
                "justification": "Noted in recent film sessions and data-led tactical roundups.",
                "source_count": 4,
            },
        ]
    )


def test_relative_age_formats_days() -> None:
    now = datetime(2026, 2, 18, 12, 0, 0)
    assert trend_pass._relative_age(now, now) == "today"
    assert trend_pass._relative_age(now - timedelta(days=1), now) == "1 day ago"
    assert trend_pass._relative_age(now - timedelta(days=5), now) == "5 days ago"


def test_build_sources_summary_uses_long_then_short_snippets_and_labels() -> None:
    now = datetime(2026, 2, 18, 12, 0, 0)
    rows = [
        (
            f"Title {idx}",
            "x" * 1000,
            "youtube" if idx % 2 else "rss",
            now - timedelta(days=idx),
            None,
        )
        for idx in range(11)
    ]

    summary = trend_pass._build_sources_summary(rows, now)
    lines = summary.splitlines()

    assert len(lines) == 11
    assert lines[0].startswith("- [ARTICLE | today] Title 0")
    assert lines[1].startswith("- [TRANSCRIPT | 1 day ago] Title 1")
    assert lines[9].count("x") == trend_pass._LONG_SNIPPET_CHARS
    assert lines[10].count("x") == trend_pass._CONTENT_SNIPPET_CHARS


def test_build_source_activity_summary_formats_buckets_and_expanded_window() -> None:
    now = datetime(2026, 2, 18, 12, 0, 0)
    rows = [
        ("a1", "", "rss", now - timedelta(days=0), None),
        ("a2", "", "rss", now - timedelta(days=0), None),
        ("a3", "", "rss", now - timedelta(days=1), None),
        ("t1", "", "youtube", now - timedelta(days=0), None),
        ("t2", "", "youtube", now - timedelta(days=1), None),
        ("a4", "", "rss", now - timedelta(days=3), None),
        ("a5", "", "rss", now - timedelta(days=4), None),
        ("a6", "", "rss", now - timedelta(days=5), None),
        ("a7", "", "rss", now - timedelta(days=5), None),
        ("a8", "", "rss", now - timedelta(days=6), None),
        ("t3", "", "youtube", now - timedelta(days=3), None),
    ]

    summary_7 = trend_pass._build_source_activity_summary(rows, now, 7)
    summary_14 = trend_pass._build_source_activity_summary(rows, now, 14)

    assert "last 2 days — articles: 3, transcripts: 2" in summary_7
    assert "3–7 days ago — articles: 5, transcripts: 1" in summary_7
    assert "3–14 days ago — articles: 5, transcripts: 1" in summary_14


def test_parse_trend_candidates_validation_matrix() -> None:
    valid = trend_pass._parse_trend_candidates(_valid_candidates_payload())
    assert len(valid) == 3
    assert all(isinstance(candidate, trend_pass.TrendCandidate) for candidate in valid)

    malformed = trend_pass._parse_trend_candidates("{not-json")
    assert malformed == []

    missing_keys = trend_pass._parse_trend_candidates('[{"rank":1,"topic":"x"}]')
    assert missing_keys == []

    one_candidate = trend_pass._parse_trend_candidates(
        '[{"rank":1,"topic":"a b c d e","justification":"j","source_count":1}]'
    )
    assert one_candidate == []


def test_validate_topic_rules_and_fallback_not_in_bad_patterns() -> None:
    four_words = "one two three four"
    twenty_six_words = " ".join(f"word{i}" for i in range(26))
    valid_ten_words = "Compact rest-defense rotations protecting central lanes after wide overload entries"

    assert trend_pass._validate_topic(valid_ten_words)
    assert not trend_pass._validate_topic(four_words)
    assert not trend_pass._validate_topic(twenty_six_words)
    assert not trend_pass._validate_topic("football analysis")
    assert trend_pass._FALLBACK_TOPIC not in trend_pass._KNOWN_BAD_PATTERNS


def test_normalise_text_lowercases_strips_punctuation_and_collapses_spaces() -> None:
    assert trend_pass._normalise_text("  Pressing, TRIGGERS!!!   in   Restarts ") == "pressing triggers in restarts"


def test_is_exact_duplicate_behaviour() -> None:
    assert trend_pass._is_exact_duplicate("Topic A", ["Topic A"])
    assert not trend_pass._is_exact_duplicate("Topic A with detail", ["Different but related topic"])
    assert trend_pass._is_exact_duplicate(
        "Final: Pressing Triggers in Possession-Restart Sequences!",
        ["final pressing triggers in possession restart sequences"],
    )


def test_cosine_similarity_identity_and_orthogonal() -> None:
    assert trend_pass._cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert trend_pass._cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_is_duplicate_two_tier_flow(monkeypatch) -> None:
    embed_calls: list[list[str]] = []

    def fake_embed(texts, _settings):
        embed_calls.append(texts)
        return [[1.0, 0.0], [0.9, 0.1]]

    monkeypatch.setattr(trend_pass, "_embed_texts", fake_embed)

    exact_duplicate, exact_score = trend_pass._is_duplicate("Topic A", ["Topic A"], settings=SimpleNamespace())
    assert exact_duplicate is True
    assert exact_score is None
    assert embed_calls == []

    semantic_duplicate, semantic_score = trend_pass._is_duplicate(
        "Topic B",
        ["Historical Topic"],
        settings=SimpleNamespace(),
    )
    assert semantic_duplicate is True
    assert semantic_score == pytest.approx(0.9)
    assert embed_calls == [["Topic B", "Historical Topic"]]


def test_run_trend_pass_dedup_scoring_and_all_duplicates_fallback(monkeypatch) -> None:
    rows = [("t", "content", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 15
    dedup_results = iter([(True, 0.91), (False, 0.40)])

    monkeypatch.setattr(trend_pass, "_query_sources", lambda *_a, **_k: rows)
    monkeypatch.setattr(trend_pass, "_fetch_recent_report_topics", lambda *_a, **_k: ["Historical"])
    monkeypatch.setattr(trend_pass, "Anthropic", lambda api_key: _FakeAnthropic(api_key, text=_valid_candidates_payload()))
    monkeypatch.setattr(trend_pass, "_is_duplicate", lambda *_a, **_k: next(dedup_results))

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    result = trend_pass.run_trend_pass(connection=object(), settings=settings)

    assert result.topic.startswith("Delayed striker decoy")
    assert result.dedup_max_similarity == pytest.approx(0.91)

    monkeypatch.setattr(trend_pass, "_is_duplicate", lambda *_a, **_k: (True, 0.92))
    all_dup_result = trend_pass.run_trend_pass(connection=object(), settings=settings)
    assert all_dup_result.topic.startswith("Narrow fullback inversion")
    assert all_dup_result.dedup_max_similarity == pytest.approx(0.92)


def test_run_trend_pass_requeries_with_expanded_lookback(monkeypatch) -> None:
    calls: list[int] = []

    def fake_query_sources(_connection, lookback_days: int):
        calls.append(lookback_days)
        if lookback_days == 7:
            return [("short", "text", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 5
        return [("expanded", "text", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 15

    monkeypatch.setattr(trend_pass, "_query_sources", fake_query_sources)
    monkeypatch.setattr(trend_pass, "_fetch_recent_report_topics", lambda *_a, **_k: [])
    monkeypatch.setattr(trend_pass, "_is_duplicate", lambda *_a, **_k: (False, 0.31))
    monkeypatch.setattr(trend_pass, "Anthropic", lambda api_key: _FakeAnthropic(api_key, text=_valid_candidates_payload()))

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    result = trend_pass.run_trend_pass(connection=object(), settings=settings, lookback_days=7)

    assert calls == [7, 14]
    assert result.lookback_days == 14
    assert result.dedup_max_similarity == pytest.approx(0.31)


def test_run_trend_pass_returns_fallback_when_no_rows_and_bypasses_validation(monkeypatch) -> None:
    monkeypatch.setattr(trend_pass, "_query_sources", lambda *_args, **_kwargs: [])

    def fail_validate(_topic: str) -> bool:
        raise AssertionError("_validate_topic should not be called for fallback path")

    monkeypatch.setattr(trend_pass, "_validate_topic", fail_validate)

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    result = trend_pass.run_trend_pass(connection=object(), settings=settings, lookback_days=7)

    assert result.topic == trend_pass._FALLBACK_TOPIC
    assert result.lookback_days == 14
    assert result.dedup_max_similarity is None


def test_validation_and_reprompt_flow(monkeypatch) -> None:
    rows = [("t", "content", "rss", datetime(2026, 2, 18, 10, 0, 0), None)] * 15
    payload = json.dumps(
        [
            {"rank": 1, "topic": "football analysis", "justification": "bad", "source_count": 3},
            {
                "rank": 2,
                "topic": "Delayed striker decoy runs opening half-space cutback lanes for opposite-side wingers",
                "justification": "good",
                "source_count": 3,
            },
        ]
    )

    monkeypatch.setattr(trend_pass, "_query_sources", lambda *_a, **_k: rows)
    monkeypatch.setattr(trend_pass, "_fetch_recent_report_topics", lambda *_a, **_k: [])
    monkeypatch.setattr(trend_pass, "_is_duplicate", lambda *_a, **_k: (False, 0.22))
    monkeypatch.setattr(trend_pass, "Anthropic", lambda api_key: _FakeAnthropic(api_key, text=payload))

    settings = SimpleNamespace(anthropic_api_key="k", anthropic_trend_model_id="m")
    monkeypatch.setattr(
        trend_pass,
        "_reprompt_for_topic",
        lambda *_a, **_k: "Specific rest-defense staggering against narrow transition counters in midfield",
    )
    result = trend_pass.run_trend_pass(connection=object(), settings=settings)
    assert result.topic.startswith("Specific rest-defense")

    monkeypatch.setattr(trend_pass, "_reprompt_for_topic", lambda *_a, **_k: "bad")
    next_candidate_result = trend_pass.run_trend_pass(connection=object(), settings=settings)
    assert next_candidate_result.topic.startswith("Delayed striker decoy")

    invalid_payload = json.dumps(
        [
            {"rank": 1, "topic": "bad", "justification": "bad", "source_count": 1},
            {"rank": 2, "topic": "also bad", "justification": "bad", "source_count": 1},
        ]
    )
    monkeypatch.setattr(trend_pass, "Anthropic", lambda api_key: _FakeAnthropic(api_key, text=invalid_payload))
    with pytest.raises(trend_pass.TrendPassError) as exc_info:
        trend_pass.run_trend_pass(connection=object(), settings=settings)
    assert len(exc_info.value.candidates_tried) >= 2


def test_trend_pass_error_carries_candidates_tried_data() -> None:
    candidates_tried = [
        {"topic": "a", "reason": "validation_failed", "max_similarity": None},
        {"topic": "b", "reason": "dedup_semantic", "max_similarity": 0.88},
        {"topic": "c", "reason": "reprompt_failed", "max_similarity": None},
    ]
    error = trend_pass.TrendPassError("failed", candidates_tried)

    assert len(error.candidates_tried) == 3
    assert {"topic", "reason"}.issubset(error.candidates_tried[0].keys())


class _FakeCursor:
    def __init__(self) -> None:
        self.results: list[object] = [(1,)] * 3

    def execute(self, *_args, **_kwargs) -> None:
        return None

    def fetchone(self):
        return self.results.pop(0) if self.results else (1,)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeConnection:
    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakePsycopg:
    @staticmethod
    def connect(_dsn: str) -> _FakeConnection:
        return _FakeConnection()


def test_run_generation_catches_trend_pass_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", _FakePsycopg)
    monkeypatch.setattr(
        pipeline,
        "load_settings",
        lambda: SimpleNamespace(postgres_dsn="dsn", anthropic_model_id="claude-sonnet"),
    )
    monkeypatch.setattr(
        pipeline,
        "run_trend_pass",
        lambda *_a, **_k: (_ for _ in ()).throw(
            trend_pass.TrendPassError("bad", [{"topic": "x", "reason": "validation_failed", "max_similarity": None}])
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "run_research_pass",
        lambda *_a, **_k: SimpleNamespace(
            queries=["q"],
            chunks=[SimpleNamespace(text="context")],
            to_json=lambda: "{}",
        ),
    )
    monkeypatch.setattr(pipeline, "run_draft_pass", lambda *_a, **_k: "draft")
    monkeypatch.setattr(pipeline, "run_critique_pass", lambda *_a, **_k: "critique")
    monkeypatch.setattr(pipeline, "run_revision_pass", lambda *_a, **_k: "final")
    monkeypatch.setattr(pipeline, "_persist_stage_cost_metrics", lambda *_a, **_k: None)

    run_id = pipeline.run_generation(pipeline_run_id="run-1", artifacts_dir=str(tmp_path))

    assert run_id == "run-1"
