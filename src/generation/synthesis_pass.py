"""Synthesis pass that merges subagent outputs into a single report markdown."""

from __future__ import annotations

import json

from anthropic import Anthropic

from ..config import Settings
from .prompts import build_synthesis_prompt
from .sub_agent import SubAgentResult


def _extract_text(response: object) -> str:
    content = getattr(response, "content", [])
    return "".join(block.text for block in content if getattr(block, "type", "") == "text").strip()


def run_synthesis_pass(topic: str, subagent_results: list[SubAgentResult], settings: Settings) -> str:
    successful = [result for result in subagent_results if result.error is None]
    failed_angles = [result.angle for result in subagent_results if result.error is not None]

    deduped_by_chunk_id: dict[int, dict[str, object]] = {}
    for result in successful:
        for chunk in result.chunks:
            chunk_id = int(chunk["chunk_id"])
            existing = deduped_by_chunk_id.get(chunk_id)
            if existing is None or float(chunk.get("combined_score", 0.0)) > float(existing.get("combined_score", 0.0)):
                deduped_by_chunk_id[chunk_id] = chunk

    summaries = [
        {"angle": result.angle, "angle_slug": result.angle_slug, "summary": result.summary}
        for result in successful
    ]

    system_prompt, user_prompt = build_synthesis_prompt(
        topic=topic,
        subagent_summaries=json.dumps(summaries, indent=2, sort_keys=True),
        chunks_json=json.dumps(list(deduped_by_chunk_id.values()), indent=2, sort_keys=True),
        failed_angles=json.dumps(failed_angles),
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.anthropic_model_id,
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    markdown = _extract_text(response)

    if failed_angles:
        missing_mentions = [angle for angle in failed_angles if angle.lower() not in markdown.lower()]
        if missing_mentions:
            markdown += (
                "\n\n---\n\n## Coverage Notes\n"
                "The following planned angles could not be fully analysed due to subagent failures: "
                f"{', '.join(missing_mentions)}."
            )

    return markdown
