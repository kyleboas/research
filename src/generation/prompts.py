"""Prompt templates for multi-pass report generation."""

from __future__ import annotations

from typing import Final

CITATION_REQUIREMENTS: Final[str] = (
    "Citation requirements:\n"
    "- Every substantive claim must include at least one inline citation.\n"
    "- Citation format is [S<source_id>:C<chunk_id>] using numeric IDs from context.\n"
    "- Do not cite sources or chunks that are not present in the provided context packet.\n"
    "- If evidence is weak or missing, explicitly state uncertainty instead of fabricating."
)

STABLE_SYSTEM_PREFIX: Final[str] = (
    "You are an evidence-grounded research writing assistant.\n"
    "You must be faithful to provided evidence and avoid unsupported claims.\n"
    f"{CITATION_REQUIREMENTS}"
)


RESEARCH_USER_TEMPLATE: Final[str] = (
    "Task topic:\n{topic}\n\n"
    "You are planning retrieval for a report. Return a JSON object with key `queries` "
    "containing 4-8 focused retrieval queries that together cover: major developments, "
    "methods, risks/limitations, and practical implications. Keep each query concise."
)

DRAFT_USER_TEMPLATE: Final[str] = (
    "Task topic:\n{topic}\n\n"
    "Context packet (JSON):\n{context_packet}\n\n"
    "Write a markdown report draft with sections: Executive Summary, Key Findings, "
    "Evidence Notes, and Open Questions. Each finding must include inline citations "
    "using the required format."
)

CRITIQUE_USER_TEMPLATE: Final[str] = (
    "Task topic:\n{topic}\n\n"
    "Context packet (JSON):\n{context_packet}\n\n"
    "Draft markdown:\n{draft_markdown}\n\n"
    "Evaluate the draft using only provided context. Return markdown with sections: "
    "Grounding Assessment, Hallucination Risks, Missing Evidence, and Revision Deltas. "
    "Each issue should point to exact sentence snippets and impacted citations."
)

REVISION_USER_TEMPLATE: Final[str] = (
    "Task topic:\n{topic}\n\n"
    "Context packet (JSON):\n{context_packet}\n\n"
    "Draft markdown:\n{draft_markdown}\n\n"
    "Critique markdown:\n{critique_markdown}\n\n"
    "Produce a final revised markdown report that incorporates critique deltas, preserves "
    "grounded claims, removes unsupported statements, and keeps inline citations compliant."
)


TREND_SYSTEM: Final[str] = (
    "You are a football research analyst specialising in identifying emerging tactical and strategic trends. "
    "Your task is to surface patterns that are gaining momentum across multiple sources — ideas being discussed "
    "by analysts, coaches, and reporters that signal a shift in how the game is evolving. "
    "Avoid obvious or already-mainstream topics. Focus on what is new and gaining traction."
)

TREND_USER_TEMPLATE: Final[str] = (
    "Here are titles and excerpts from recent football articles and transcripts:\n\n"
    "{sources_summary}\n\n"
    "Recent topic history to avoid repeating:\n"
    "{recent_topics_block}\n\n"
    "Source activity summary:\n"
    "{source_activity_summary}\n\n"
    "Identify 3-5 emerging football trend candidates and return them as a JSON array.\n"
    "Each array object must contain exactly these keys:\n"
    '- `rank` (int)\n'
    '- `topic` (10-20 word phrase)\n'
    '- `justification` (25 words max)\n'
    '- `source_count` (int)\n\n'
    "## Ranking criteria\n"
    "Rank candidates by weighing:\n"
    "1) Velocity: mentions accelerating in the last 2 days versus the prior 5 days.\n"
    "2) Cross-source convergence: appears across both [ARTICLE] and [TRANSCRIPT] sources.\n"
    "3) First-appearance recency: earliest appearance is within the last 48 hours.\n"
    "Rank lower any topic discussed at a flat rate across the whole window.\n\n"
    "Return only valid JSON; do not include markdown fences or additional prose."
)

TREND_REPROMPT_USER_TEMPLATE: Final[str] = (
    "The previous topic phrase was rejected for being too broad or malformed:\n"
    "\"{rejected_phrase}\"\n\n"
    "Provide one more specific replacement topic phrase in 10-20 words.\n"
    "Return plain text only."
)


def build_trend_prompt(
    *,
    sources_summary: str,
    recent_topics_block: str,
    source_activity_summary: str,
) -> tuple[str, str]:
    return TREND_SYSTEM, TREND_USER_TEMPLATE.format(
        sources_summary=sources_summary,
        recent_topics_block=recent_topics_block,
        source_activity_summary=source_activity_summary,
    )


def build_trend_reprompt(*, rejected_phrase: str) -> tuple[str, str]:
    return TREND_SYSTEM, TREND_REPROMPT_USER_TEMPLATE.format(rejected_phrase=rejected_phrase)


def build_research_prompt(topic: str) -> tuple[str, str]:
    return STABLE_SYSTEM_PREFIX, RESEARCH_USER_TEMPLATE.format(topic=topic)


def build_draft_prompt(*, topic: str, context_packet: str) -> tuple[str, str]:
    return STABLE_SYSTEM_PREFIX, DRAFT_USER_TEMPLATE.format(topic=topic, context_packet=context_packet)


def build_critique_prompt(*, topic: str, context_packet: str, draft_markdown: str) -> tuple[str, str]:
    return STABLE_SYSTEM_PREFIX, CRITIQUE_USER_TEMPLATE.format(
        topic=topic,
        context_packet=context_packet,
        draft_markdown=draft_markdown,
    )


def build_revision_prompt(
    *,
    topic: str,
    context_packet: str,
    draft_markdown: str,
    critique_markdown: str,
) -> tuple[str, str]:
    return STABLE_SYSTEM_PREFIX, REVISION_USER_TEMPLATE.format(
        topic=topic,
        context_packet=context_packet,
        draft_markdown=draft_markdown,
        critique_markdown=critique_markdown,
    )
