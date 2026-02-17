"""Draft pass for evidence-grounded markdown generation."""

from __future__ import annotations

from anthropic import Anthropic

from ..config import Settings
from .prompts import build_draft_prompt
from .research_pass import ContextPacket


def run_draft_pass(*, topic: str, context_packet: ContextPacket, settings: Settings, max_tokens: int = 1800) -> str:
    """Produce markdown draft with inline source/chunk citations."""

    system_prompt, user_prompt = build_draft_prompt(topic=topic, context_packet=context_packet.to_json())

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_model_id,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()
