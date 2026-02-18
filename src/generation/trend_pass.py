"""Trend discovery pass: identify emerging football topics from recent ingested sources."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from dataclasses import dataclass

from anthropic import Anthropic

from ..config import Settings
from .prompts import build_trend_prompt, build_trend_reprompt

_FALLBACK_TOPIC = "emerging football tactical trends"

# Maximum characters of content to include per source in the summary sent to the LLM.
_CONTENT_SNIPPET_CHARS = 300
_LONG_SNIPPET_CHARS = 800
_LONG_SNIPPET_TOP_N = 10
_DEDUP_SIMILARITY_THRESHOLD = 0.85
_KNOWN_BAD_PATTERNS: frozenset[str] = frozenset({"football analysis", "tactical trends"})

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


def _fetch_recent_report_topics(connection: object, limit: int = 10) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT title
            FROM reports
            WHERE report_type = 'final'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cursor.fetchall()

    topics: list[str] = []
    for (title,) in rows:
        if isinstance(title, str) and title.strip():
            topics.append(title.strip())
    return topics


def _normalise_text(text: str) -> str:
    lowered = text.lower()
    without_punctuation = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def _is_exact_duplicate(candidate: str, historical_topics: list[str]) -> bool:
    candidate_norm = _normalise_text(candidate)
    if not candidate_norm:
        return False
    return any(candidate_norm == _normalise_text(topic) for topic in historical_topics)


def _embed_texts(
    texts: list[str],
    settings: Settings,
    *,
    max_retries: int = 4,
    initial_backoff_s: float = 1.0,
) -> list[list[float]]:
    if not texts:
        return []

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    for attempt in range(max_retries + 1):
        try:
            response = client.embeddings.create(model=settings.openai_embedding_model, input=texts)
            return [list(row.embedding) for row in response.data]
        except Exception as error:  # noqa: BLE001
            if attempt >= max_retries:
                raise

            delay = initial_backoff_s * (2**attempt)
            jitter = random.uniform(0, delay * 0.2)
            sleep_seconds = delay + jitter
            logger.warning(
                "Trend dedup embedding API error on attempt %s/%s; retrying in %.2fs: %s",
                attempt + 1,
                max_retries + 1,
                sleep_seconds,
                error,
            )
            time.sleep(sleep_seconds)

    return []


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    return sum(left * right for left, right in zip(a, b, strict=False))


def _is_semantic_duplicate(
    candidate_topic: str,
    historical_topics: list[str],
    settings: Settings,
) -> tuple[bool, float]:
    if not historical_topics:
        return False, 0.0

    vectors = _embed_texts([candidate_topic, *historical_topics], settings)
    if len(vectors) < 2:
        return False, 0.0

    candidate_vector = vectors[0]
    scores = [_cosine_similarity(candidate_vector, historical_vector) for historical_vector in vectors[1:]]
    max_score = max(scores) if scores else 0.0
    return max_score >= _DEDUP_SIMILARITY_THRESHOLD, max_score


def _is_duplicate(
    candidate_topic: str,
    historical_topics: list[str],
    settings: Settings,
) -> tuple[bool, float | None]:
    if _is_exact_duplicate(candidate_topic, historical_topics):
        return True, None
    duplicate, score = _is_semantic_duplicate(candidate_topic, historical_topics, settings)
    return duplicate, score


def _validate_topic(topic: str) -> bool:
    words = [word for word in topic.split() if word]
    if not (5 <= len(words) <= 25):
        return False
    lowered = topic.strip().lower()
    return lowered not in _KNOWN_BAD_PATTERNS


def _reprompt_for_topic(rejected_phrase: str, settings: Settings, client: Anthropic) -> str:
    system_prompt, user_prompt = build_trend_reprompt(rejected_phrase=rejected_phrase)
    try:
        response = client.messages.create(
            model=settings.anthropic_trend_model_id,
            max_tokens=120,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception:  # noqa: BLE001
        return ""

    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()


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
    recent_topics = _fetch_recent_report_topics(connection)
    recent_topics_block = "None"
    if recent_topics:
        recent_topics_block = "\n".join(f"- {topic}" for topic in recent_topics)

    system_prompt, user_prompt = build_trend_prompt(
        sources_summary=sources_summary,
        recent_topics_block=recent_topics_block,
        source_activity_summary=source_activity_summary,
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_trend_model_id,
        max_tokens=600,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_response = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()
    candidates = _parse_trend_candidates(raw_response)

    candidates_tried: list[dict] = []
    dedup_max_similarity: float | None = None
    if not candidates:
        candidates_tried.append({"topic": raw_response, "reason": "validation_failed", "max_similarity": None})
        raise TrendPassError("Trend pass returned no valid candidates", candidates_tried)

    selected_topic: str | None = None
    for candidate in candidates:
        is_duplicate, similarity_score = _is_duplicate(candidate.topic, recent_topics, settings)
        logger.info(
            "Trend candidate '%s' dedup checked: duplicate=%s similarity=%s",
            candidate.topic,
            is_duplicate,
            similarity_score,
        )
        if similarity_score is not None:
            dedup_max_similarity = (
                similarity_score
                if dedup_max_similarity is None
                else max(dedup_max_similarity, similarity_score)
            )
        if is_duplicate:
            candidates_tried.append(
                {
                    "topic": candidate.topic,
                    "reason": "dedup_exact" if similarity_score is None else "dedup_semantic",
                    "max_similarity": similarity_score,
                }
            )
            continue

        if _validate_topic(candidate.topic):
            selected_topic = candidate.topic
            break

        candidates_tried.append(
            {"topic": candidate.topic, "reason": "validation_failed", "max_similarity": similarity_score}
        )
        reprompted_topic = _reprompt_for_topic(candidate.topic, settings, client)
        if not reprompted_topic or not _validate_topic(reprompted_topic):
            candidates_tried.append(
                {"topic": reprompted_topic or candidate.topic, "reason": "reprompt_failed", "max_similarity": None}
            )
            continue

        reprompt_duplicate, reprompt_similarity = _is_duplicate(reprompted_topic, recent_topics, settings)
        logger.info(
            "Trend reprompt candidate '%s' dedup checked: duplicate=%s similarity=%s",
            reprompted_topic,
            reprompt_duplicate,
            reprompt_similarity,
        )
        if reprompt_similarity is not None:
            dedup_max_similarity = (
                reprompt_similarity
                if dedup_max_similarity is None
                else max(dedup_max_similarity, reprompt_similarity)
            )
        if reprompt_duplicate:
            candidates_tried.append(
                {
                    "topic": reprompted_topic,
                    "reason": "dedup_exact" if reprompt_similarity is None else "dedup_semantic",
                    "max_similarity": reprompt_similarity,
                }
            )
            continue

        selected_topic = reprompted_topic
        break

    if selected_topic is None:
        if candidates and all(entry["reason"].startswith("dedup") for entry in candidates_tried):
            logger.warning("All trend candidates were duplicates; falling back to candidate #1")
            selected_topic = candidates[0].topic
        else:
            raise TrendPassError("No valid trend candidates survived filtering", candidates_tried)

    return TrendPassResult(
        topic=selected_topic,
        candidates=candidates,
        lookback_days=actual_lookback_days,
        dedup_max_similarity=dedup_max_similarity,
    )
