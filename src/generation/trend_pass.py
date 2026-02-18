"""Trend discovery pass: identify emerging football topics from recent ingested sources."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from dataclasses import dataclass

from anthropic import Anthropic

from ..config import Settings
from .prompts import build_trend_prompt

_FALLBACK_TOPIC = "emerging football tactical trends"

# Maximum characters of content to include per source in the summary sent to the LLM.
_CONTENT_SNIPPET_CHARS = 300
_LONG_SNIPPET_CHARS = 800
_LONG_SNIPPET_TOP_N = 10

# Maximum number of sources to include in the trend discovery prompt.
_MAX_SOURCES = 60
_MIN_SOURCES_THRESHOLD = 10

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TrendCandidate:
    rank: int
    topic: str
    justification: str
    source_count: int


@dataclass(slots=True)
class TrendPassResult:
    topic: str
    candidates: list[TrendCandidate]
    lookback_days: int
    dedup_max_similarity: float | None


class TrendPassError(Exception):
    def __init__(self, message: str, candidates_tried: list[dict]):
        super().__init__(message)
        self.candidates_tried = candidates_tried


def _parse_trend_candidates(raw: str) -> list[TrendCandidate]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(payload, list) or len(payload) < 2:
        return []

    candidates: list[TrendCandidate] = []
    for item in payload:
        if not isinstance(item, dict):
            return []
        if set(item) != {"rank", "topic", "justification", "source_count"}:
            return []

        rank = item.get("rank")
        topic = item.get("topic")
        justification = item.get("justification")
        source_count = item.get("source_count")
        if not isinstance(rank, int):
            return []
        if not isinstance(topic, str):
            return []
        if not isinstance(justification, str):
            return []
        if not isinstance(source_count, int):
            return []

        candidates.append(
            TrendCandidate(
                rank=rank,
                topic=topic,
                justification=justification,
                source_count=source_count,
            )
        )

    return candidates


def _query_sources(connection: object, lookback_days: int) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT title, metadata ->> 'content', source_type, created_at, published_at
            FROM sources
            WHERE published_at >= NOW() - %s::interval
               OR (published_at IS NULL AND created_at >= NOW() - %s::interval)
            ORDER BY COALESCE(published_at, created_at) DESC
            LIMIT %s
            """,
            (f"{lookback_days} days", f"{lookback_days} days", _MAX_SOURCES),
        )
        return cursor.fetchall()


def _relative_age(dt: datetime, now: datetime) -> str:
    age_days = max((now.date() - dt.date()).days, 0)
    if age_days == 0:
        return "today"
    if age_days == 1:
        return "1 day ago"
    return f"{age_days} days ago"


def _source_type_label(source_type: str | None) -> str:
    return "[TRANSCRIPT]" if source_type == "youtube" else "[ARTICLE]"


def _build_sources_summary(rows: list[tuple], now: datetime) -> str:
    if not rows:
        return ""

    sorted_rows = sorted(
        rows,
        key=lambda row: row[4] or row[3],
        reverse=True,
    )

    lines: list[str] = []
    for index, (title, content, source_type, created_at, published_at) in enumerate(sorted_rows):
        snippet_size = _LONG_SNIPPET_CHARS if index < _LONG_SNIPPET_TOP_N else _CONTENT_SNIPPET_CHARS
        snippet = (content or "").strip()[:snippet_size]
        if not title and not snippet:
            continue

        observed_at = published_at or created_at
        if observed_at is None:
            continue

        label = _source_type_label(source_type)
        age = _relative_age(observed_at, now)
        source_title = title or "Untitled"
        line = f"- [{label.strip('[]')} | {age}] {source_title}"
        if snippet:
            line += f": {snippet}"
        lines.append(line)

    return "\n".join(lines)


def _build_source_activity_summary(rows: list[tuple], now: datetime, lookback_days: int) -> str:
    last_2_articles = 0
    last_2_transcripts = 0
    prior_articles = 0
    prior_transcripts = 0

    for _, _, source_type, created_at, published_at in rows:
        observed_at = published_at or created_at
        if observed_at is None:
            continue

        age_days = max((now.date() - observed_at.date()).days, 0)
        is_transcript = source_type == "youtube"
        if age_days <= 1:
            if is_transcript:
                last_2_transcripts += 1
            else:
                last_2_articles += 1
        else:
            if is_transcript:
                prior_transcripts += 1
            else:
                prior_articles += 1

    return (
        f"last 2 days — articles: {last_2_articles}, transcripts: {last_2_transcripts}\n"
        f"3–{lookback_days} days ago — articles: {prior_articles}, transcripts: {prior_transcripts}"
    )


def run_trend_pass(
    connection: object,
    *,
    settings: Settings,
    lookback_days: int = 7,
) -> TrendPassResult:
    """Scan recent ingested sources and ask the LLM to identify the top emerging football trend.

    Returns a concise topic phrase plus trend metadata for downstream logging.
    Falls back to a generic topic payload if no recent sources are found.
    """
    actual_lookback_days = lookback_days
    rows = _query_sources(connection, lookback_days)
    if len(rows) < _MIN_SOURCES_THRESHOLD:
        actual_lookback_days = lookback_days * 2
        logger.info(
            "Trend pass found only %s sources in %s-day window; retrying with %s-day window",
            len(rows),
            lookback_days,
            actual_lookback_days,
        )
        rows = _query_sources(connection, actual_lookback_days)

    if not rows:
        return TrendPassResult(topic=_FALLBACK_TOPIC, candidates=[], lookback_days=actual_lookback_days, dedup_max_similarity=None)

    now = datetime.now()
    sources_summary = _build_sources_summary(rows, now)
    if not sources_summary:
        return TrendPassResult(topic=_FALLBACK_TOPIC, candidates=[], lookback_days=actual_lookback_days, dedup_max_similarity=None)
    source_activity_summary = _build_source_activity_summary(rows, now, actual_lookback_days)

    system_prompt, user_prompt = build_trend_prompt(
        sources_summary=sources_summary,
        recent_topics_block="None",
        source_activity_summary=source_activity_summary,
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_trend_model_id,
        max_tokens=100,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    topic = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()

    return TrendPassResult(
        topic=topic or _FALLBACK_TOPIC,
        candidates=[],
        lookback_days=actual_lookback_days,
        dedup_max_similarity=None,
    )
