"""Trend discovery pass: identify emerging football topics from recent ingested sources."""

from __future__ import annotations

from datetime import timedelta

from anthropic import Anthropic

from ..config import Settings
from .prompts import build_trend_prompt

_FALLBACK_TOPIC = "emerging football tactical trends"

# Maximum characters of content to include per source in the summary sent to the LLM.
_CONTENT_SNIPPET_CHARS = 300

# Maximum number of sources to include in the trend discovery prompt.
_MAX_SOURCES = 60


def run_trend_pass(
    connection: object,
    *,
    settings: Settings,
    lookback_days: int = 7,
) -> str:
    """Scan recent ingested sources and ask the LLM to identify the top emerging football trend.

    Returns a concise topic phrase suitable for use as the report topic.
    Falls back to a generic topic string if no recent sources are found.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT title, metadata ->> 'content'
            FROM sources
            WHERE published_at >= NOW() - %s::interval
               OR (published_at IS NULL AND created_at >= NOW() - %s::interval)
            ORDER BY COALESCE(published_at, created_at) DESC
            LIMIT %s
            """,
            (f"{lookback_days} days", f"{lookback_days} days", _MAX_SOURCES),
        )
        rows = cursor.fetchall()

    if not rows:
        return _FALLBACK_TOPIC

    lines: list[str] = []
    for title, content in rows:
        snippet = (content or "").strip()[:_CONTENT_SNIPPET_CHARS]
        if title:
            entry = f"- {title}"
            if snippet:
                entry += f": {snippet}"
            lines.append(entry)

    if not lines:
        return _FALLBACK_TOPIC

    sources_summary = "\n".join(lines)
    system_prompt, user_prompt = build_trend_prompt(sources_summary=sources_summary)

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_model_id,
        max_tokens=100,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    topic = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()

    return topic or _FALLBACK_TOPIC
