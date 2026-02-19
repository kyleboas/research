"""Parallelisable sub-agent retrieval + summarisation worker."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
import logging
import re
import time

from anthropic import Anthropic
from openai import OpenAI

from ..config import Settings
from ..processing.retrieval import RetrievedChunk, hybrid_search
from .lead_agent import TaskDescription
from .prompts import build_subagent_eval_prompt, build_subagent_prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchRound:
    round_number: int
    query: str
    chunks_retrieved: list[dict[str, object]]
    chunk_count: int
    evaluation: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SubAgentResult:
    angle: str
    angle_slug: str
    chunks: list[dict[str, object]]
    summary: str
    citations: list[str]
    search_trajectory: list[SearchRound]
    total_rounds: int
    elapsed_s: float
    input_tokens: int
    output_tokens: int
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "angle": self.angle,
            "angle_slug": self.angle_slug,
            "chunks": self.chunks,
            "summary": self.summary,
            "citations": self.citations,
            "search_trajectory": [round_result.to_dict() for round_result in self.search_trajectory],
            "total_rounds": self.total_rounds,
            "elapsed_s": self.elapsed_s,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
        }


def _chunk_to_dict(chunk: RetrievedChunk) -> dict[str, object]:
    return {
        "source_id": chunk.source_id,
        "chunk_id": chunk.chunk_id,
        "chunk_index": chunk.chunk_index,
        "citation_id": f"S{chunk.source_id}:C{chunk.chunk_id}",
        "source_type": chunk.source_type,
        "source_key": chunk.source_key,
        "title": chunk.source_title,
        "combined_score": chunk.combined_score,
        "text": chunk.content,
    }


def _extract_text(response: object) -> str:
    content = getattr(response, "content", [])
    return "".join(block.text for block in content if getattr(block, "type", "") == "text").strip()


def _run_search_round(query: str, connection: object, settings: Settings) -> list[RetrievedChunk]:
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.embeddings.create(model=settings.openai_embedding_model, input=[query])
    query_embedding = list(response.data[0].embedding)
    return hybrid_search(connection, query_text=query, query_embedding=query_embedding, top_k=8)


def _evaluate_search_results(task: TaskDescription, all_chunks: list[dict[str, object]], round_number: int, settings: Settings) -> dict[str, object]:
    fallback = {"sufficient": True, "gaps": [], "next_query": None}
    chunks_json = json.dumps(all_chunks, sort_keys=True)
    system_prompt, user_prompt = build_subagent_eval_prompt(
        angle=task.angle,
        objective=task.objective,
        chunks_json=chunks_json,
        round_number=round_number,
    )

    try:
        client = Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_model_id,
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        parsed = json.loads(_extract_text(response))
        if not isinstance(parsed, dict):
            return fallback
        return {
            "sufficient": bool(parsed.get("sufficient", True)),
            "gaps": parsed.get("gaps") if isinstance(parsed.get("gaps"), list) else [],
            "next_query": parsed.get("next_query") if isinstance(parsed.get("next_query"), str) else None,
        }
    except Exception as error:  # noqa: BLE001
        logger.warning("Subagent evaluation failed for %s round %s: %s", task.angle_slug, round_number, error)
        return fallback


def _extract_citations(markdown: str) -> list[str]:
    matches = re.findall(r"\[S\d+:C\d+\]", markdown)
    return sorted(set(matches))


def run_subagent(task: TaskDescription, postgres_dsn: str, settings: Settings, max_search_rounds: int = 3) -> SubAgentResult:
    start = time.perf_counter()
    input_tokens = 0
    output_tokens = 0
    try:
        import psycopg

        all_chunks_by_id: dict[int, dict[str, object]] = {}
        trajectory: list[SearchRound] = []
        query = task.search_guidance

        with psycopg.connect(postgres_dsn) as connection:
            for round_number in range(1, max_search_rounds + 1):
                round_results = _run_search_round(query, connection, settings)
                round_chunks = [_chunk_to_dict(chunk) for chunk in round_results]
                for chunk in round_chunks:
                    existing = all_chunks_by_id.get(int(chunk["chunk_id"]))
                    if existing is None or float(chunk.get("combined_score", 0.0)) > float(existing.get("combined_score", 0.0)):
                        all_chunks_by_id[int(chunk["chunk_id"])] = chunk

                all_chunks = list(all_chunks_by_id.values())
                evaluation = _evaluate_search_results(task, all_chunks, round_number, settings)
                trajectory.append(
                    SearchRound(
                        round_number=round_number,
                        query=query,
                        chunks_retrieved=round_chunks,
                        chunk_count=len(round_chunks),
                        evaluation=evaluation,
                    )
                )

                if bool(evaluation.get("sufficient")) or round_number >= max_search_rounds:
                    break

                next_query = evaluation.get("next_query")
                if not isinstance(next_query, str) or next_query.strip() == "":
                    break
                query = next_query

        all_chunks = list(all_chunks_by_id.values())
        chunks_json = json.dumps(all_chunks, sort_keys=True)
        system_prompt, user_prompt = build_subagent_prompt(
            angle=task.angle,
            objective=task.objective,
            search_guidance=task.search_guidance,
            task_boundaries=task.task_boundaries,
            chunks_json=chunks_json,
        )

        input_tokens += round((len(system_prompt) + len(user_prompt)) / 4)
        client = Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_model_id,
            max_tokens=1400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        summary = _extract_text(response)
        output_tokens += round(len(summary) / 4)

        elapsed_s = round(time.perf_counter() - start, 3)
        return SubAgentResult(
            angle=task.angle,
            angle_slug=task.angle_slug,
            chunks=all_chunks,
            summary=summary,
            citations=_extract_citations(summary),
            search_trajectory=trajectory,
            total_rounds=len(trajectory),
            elapsed_s=elapsed_s,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=None,
        )
    except Exception as error:  # noqa: BLE001
        elapsed_s = round(time.perf_counter() - start, 3)
        logger.warning("Subagent failed for %s: %s", task.angle_slug, error)
        return SubAgentResult(
            angle=task.angle,
            angle_slug=task.angle_slug,
            chunks=[],
            summary="",
            citations=[],
            search_trajectory=[],
            total_rounds=0,
            elapsed_s=elapsed_s,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=str(error),
        )


def run_parallel_subagents(
    tasks: list[TaskDescription],
    postgres_dsn: str,
    settings: Settings,
    max_search_rounds: int = 3,
) -> list[SubAgentResult]:
    if not tasks:
        return []

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [
            executor.submit(run_subagent, task, postgres_dsn, settings, max_search_rounds)
            for task in tasks
        ]
        results = [future.result() for future in futures]

    if all(result.error is not None for result in results):
        raise RuntimeError("All subagents failed; no successful subagent outputs were produced")

    for result in results:
        if result.error is not None:
            logger.warning("Subagent %s failed: %s", result.angle_slug, result.error)

    return results
