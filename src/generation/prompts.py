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
