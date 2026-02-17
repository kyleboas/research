"""Critique pass for grounding and hallucination-risk review."""

from __future__ import annotations

from anthropic import Anthropic

from ..config import Settings
from .prompts import build_critique_prompt
from .research_pass import ContextPacket


def run_critique_pass(
    *,
    topic: str,
    context_packet: ContextPacket,
    draft_markdown: str,
    settings: Settings,
    max_tokens: int = 1400,
) -> str:
    """Evaluate draft against retrieved context only and return critique markdown."""

    system_prompt, user_prompt = build_critique_prompt(
        topic=topic,
        context_packet=context_packet.to_json(),
        draft_markdown=draft_markdown,
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_small_model_id,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()
