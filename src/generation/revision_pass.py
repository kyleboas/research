"""Revision pass that applies critique deltas to produce final report markdown."""

from __future__ import annotations

from anthropic import Anthropic

from ..config import Settings
from .prompts import build_revision_prompt
from .research_pass import ContextPacket


def run_revision_pass(
    *,
    topic: str,
    context_packet: ContextPacket,
    draft_markdown: str,
    critique_markdown: str,
    settings: Settings,
    max_tokens: int = 2000,
) -> str:
    """Generate revised final markdown incorporating critique deltas."""

    system_prompt, user_prompt = build_revision_prompt(
        topic=topic,
        context_packet=context_packet.to_json(),
        draft_markdown=draft_markdown,
        critique_markdown=critique_markdown,
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_model_id,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()
