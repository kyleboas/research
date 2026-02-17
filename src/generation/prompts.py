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
    "Here are titles and excerpts from recent football articles and content:\n\n"
    "{sources_summary}\n\n"
    "Based on this content, identify the single most significant **emerging trend** in football that:\n"
    "1. Appears across multiple sources (not a one-off story)\n"
    "2. Is gaining traction but has not yet become mainstream\n"
    "3. Has tactical, strategic, or analytical significance\n\n"
    "Return ONLY a concise topic phrase (10-20 words) that captures this trend. "
    "Do not include explanations, bullet points, or any other text — just the topic phrase."
)


def build_trend_prompt(*, sources_summary: str) -> tuple[str, str]:
    return TREND_SYSTEM, TREND_USER_TEMPLATE.format(sources_summary=sources_summary)


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
