"""LLM-based report quality judging."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from anthropic import Anthropic

from ..config import Settings
from ..generation.prompts import build_llm_judge_prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeResult:
    factual_accuracy: float
    citation_accuracy: float
    completeness: float
    source_quality: float
    source_diversity: float
    overall_pass: bool

    def average_score(self) -> float:
        return (
            self.factual_accuracy
            + self.citation_accuracy
            + self.completeness
            + self.source_quality
            + self.source_diversity
        ) / 5

    def to_dict(self) -> dict[str, object]:
        return {
            "factual_accuracy": self.factual_accuracy,
            "citation_accuracy": self.citation_accuracy,
            "completeness": self.completeness,
            "source_quality": self.source_quality,
            "source_diversity": self.source_diversity,
            "overall_pass": self.overall_pass,
            "average_score": self.average_score(),
        }


def _extract_text(response: object) -> str:
    content = getattr(response, "content", [])
    return "".join(block.text for block in content if getattr(block, "type", "") == "text").strip()


def _sentinel_result() -> JudgeResult:
    return JudgeResult(
        factual_accuracy=0.0,
        citation_accuracy=0.0,
        completeness=0.0,
        source_quality=0.0,
        source_diversity=0.0,
        overall_pass=False,
    )


def run_llm_judge(report_markdown: str, chunk_texts: dict[int, str], settings: Settings) -> JudgeResult:
    try:
        chunks_json = json.dumps(
            [{"chunk_id": chunk_id, "text": text} for chunk_id, text in sorted(chunk_texts.items())],
            sort_keys=True,
        )
        system_prompt, user_prompt = build_llm_judge_prompt(report_markdown, chunks_json)

        client = Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_model_id,
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        payload = json.loads(_extract_text(response))

        required = [
            "factual_accuracy",
            "citation_accuracy",
            "completeness",
            "source_quality",
            "source_diversity",
            "overall_pass",
        ]
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"LLM judge missing keys: {', '.join(missing)}")

        scores: dict[str, float] = {}
        for key in required[:-1]:
            value = float(payload[key])
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"LLM judge score '{key}' out of range: {value}")
            scores[key] = value

        return JudgeResult(
            factual_accuracy=scores["factual_accuracy"],
            citation_accuracy=scores["citation_accuracy"],
            completeness=scores["completeness"],
            source_quality=scores["source_quality"],
            source_diversity=scores["source_diversity"],
            overall_pass=bool(payload["overall_pass"]),
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("LLM judge failed; returning sentinel result: %s", error)
        return _sentinel_result()
