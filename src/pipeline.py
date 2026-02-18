"""Pipeline entrypoints and stage orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
import time
import uuid

from .config import load_settings
from .delivery.email import send_summary_email
from .delivery.github_publish import publish_report_markdown
from .delivery.slack import post_report_summary
from .ingestion.dedupe import filter_existing_records, filter_existing_youtube_records
from .ingestion.markdown import html_to_markdown
from .ingestion.newsblur import fetch_newsblur_records, load_newsblur_config_from_env
from .ingestion.youtube import fetch_all_channels, load_youtube_channel_configs_from_env
from .generation.critique_pass import run_critique_pass
from .generation.draft_pass import run_draft_pass
from .generation.research_pass import run_research_pass
from .generation.revision_pass import run_revision_pass
from .generation.trend_pass import TrendPassResult, run_trend_pass
from .processing.chunking import chunk_text
from .processing.embeddings import embed_chunks, upsert_embeddings
from .verification.claims import extract_claims
from .verification.nli_check import check_claims_against_citations
from .verification.scoring import score_claim_results

LOGGER = logging.getLogger("research.pipeline")
if not LOGGER.handlers:
    configured_level = os.getenv("RESEARCH_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logging.basicConfig(level=getattr(logging, configured_level, logging.INFO), format="%(message)s")


EMBEDDING_COST_PER_MILLION_TOKENS_USD = float(os.getenv("EMBEDDING_COST_PER_MILLION_TOKENS_USD", "0.02"))
SONNET_INPUT_COST_PER_MILLION_TOKENS_USD = float(os.getenv("SONNET_INPUT_COST_PER_MILLION_TOKENS_USD", "3.0"))
SONNET_OUTPUT_COST_PER_MILLION_TOKENS_USD = float(os.getenv("SONNET_OUTPUT_COST_PER_MILLION_TOKENS_USD", "15.0"))
OPUS_COST_MULTIPLIER = float(os.getenv("OPUS_COST_MULTIPLIER", "5"))


def _estimate_tokens_from_text(text: str) -> int:
    normalized = text.strip()
    if normalized == "":
        return 0
    return max(1, round(len(normalized) / 4))


def _resolve_generation_model_tier(model_name: str) -> str:
    return "opus" if "opus" in model_name.lower() else "sonnet"


def _estimate_generation_step_cost(*, input_tokens: int, output_tokens: int, model_tier: str) -> float:
    multiplier = OPUS_COST_MULTIPLIER if model_tier == "opus" else 1.0
    input_cost = (input_tokens / 1_000_000) * SONNET_INPUT_COST_PER_MILLION_TOKENS_USD
    output_cost = (output_tokens / 1_000_000) * SONNET_OUTPUT_COST_PER_MILLION_TOKENS_USD
    return round((input_cost + output_cost) * multiplier, 6)


def _persist_stage_cost_metrics(
    connection: object,
    *,
    pipeline_run_id: str,
    stage: str,
    metrics: dict[str, object],
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                MAX(CASE WHEN column_name = 'run_name' THEN 1 ELSE 0 END) = 1 AS has_run_name,
                MAX(CASE WHEN column_name = 'metadata' THEN 1 ELSE 0 END) = 1 AS has_metadata,
                MAX(CASE WHEN column_name = 'cost_estimate_json' THEN 1 ELSE 0 END) = 1 AS has_cost_estimate_json
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'pipeline_runs'
              AND column_name IN ('run_name', 'metadata', 'cost_estimate_json')
            """
        )
        has_run_name_column, has_metadata_column, has_cost_column = (bool(value) for value in cursor.fetchone())

        select_columns = ["id"]
        if has_metadata_column:
            select_columns.append("metadata")
        if has_cost_column:
            select_columns.append("cost_estimate_json")

        if has_run_name_column:
            try:
                cursor.execute(
                    f"""
                    SELECT {', '.join(select_columns)}
                    FROM pipeline_runs
                    WHERE run_name = %s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (pipeline_run_id,),
                )
                row = cursor.fetchone()
            except Exception as exc:  # pragma: no cover - exercised via mocked sqlstate in tests
                if getattr(exc, "sqlstate", None) != "42703":
                    raise
                has_run_name_column = False
                if has_metadata_column:
                    cursor.execute(
                        f"""
                        SELECT {', '.join(select_columns)}
                        FROM pipeline_runs
                        WHERE metadata ->> 'pipeline_run_id' = %s
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (pipeline_run_id,),
                    )
                    row = cursor.fetchone()
                else:
                    row = None
        elif has_metadata_column:
            cursor.execute(
                f"""
                SELECT {', '.join(select_columns)}
                FROM pipeline_runs
                WHERE metadata ->> 'pipeline_run_id' = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (pipeline_run_id,),
            )
            row = cursor.fetchone()
        else:
            row = None
        expected_columns = 1 + int(has_metadata_column) + int(has_cost_column)
        if row is not None and len(row) != expected_columns:
            return

        if row is None:
            insert_columns: list[str] = ["status"]
            insert_values: list[str] = ["%s"]
            insert_params: list[object] = ["running"]

            if has_run_name_column:
                insert_columns.insert(0, "run_name")
                insert_values.insert(0, "%s")
                insert_params.insert(0, pipeline_run_id)
            if has_metadata_column:
                insert_columns.append("metadata")
                insert_values.append("%s::jsonb")
                insert_params.append(json.dumps({"pipeline_run_id": pipeline_run_id}))
            if has_cost_column:
                insert_columns.append("cost_estimate_json")
                insert_values.append("%s::jsonb")
                insert_params.append(json.dumps({}))

            try:
                cursor.execute(
                    f"""
                    INSERT INTO pipeline_runs ({', '.join(insert_columns)})
                    VALUES ({', '.join(insert_values)})
                    RETURNING id
                    """,
                    tuple(insert_params),
                )
            except Exception as exc:  # pragma: no cover - exercised via mocked sqlstate in tests
                if not has_run_name_column or getattr(exc, "sqlstate", None) != "42703":
                    raise
                insert_columns = [column for column in insert_columns if column != "run_name"]
                insert_values = insert_values[1:]
                insert_params = insert_params[1:]
                cursor.execute(
                    f"""
                    INSERT INTO pipeline_runs ({', '.join(insert_columns)})
                    VALUES ({', '.join(insert_values)})
                    RETURNING id
                    """,
                    tuple(insert_params),
                )
            run_id = cursor.fetchone()[0]
            existing_metadata: dict[str, object] = {"pipeline_run_id": pipeline_run_id}
            existing_cost: dict[str, object] = {}
        else:
            row_index = 0
            run_id = row[row_index]
            row_index += 1
            existing_metadata = row[row_index] if has_metadata_column else {}
            if has_metadata_column:
                row_index += 1
            existing_cost = row[row_index] if has_cost_column else {}
            if not isinstance(existing_metadata, dict):
                existing_metadata = {}
            if not isinstance(existing_cost, dict):
                existing_cost = {}

        stages = existing_cost.get("stages", {}) if isinstance(existing_cost, dict) else {}
        if not isinstance(stages, dict):
            stages = {}
        stages[stage] = metrics

        total_tokens = 0
        total_cost = 0.0
        for stage_metrics in stages.values():
            if not isinstance(stage_metrics, dict):
                continue
            token_count = stage_metrics.get("token_count", 0)
            estimated_cost = stage_metrics.get("estimated_cost_usd", 0.0)
            if isinstance(token_count, (int, float)):
                total_tokens += int(token_count)
            if isinstance(estimated_cost, (int, float)):
                total_cost += float(estimated_cost)

        updated_cost = {
            "stages": stages,
            "total_token_count": total_tokens,
            "total_estimated_cost_usd": round(total_cost, 6),
            "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }

        if has_cost_column:
            set_clauses = ["cost_estimate_json = %s::jsonb"]
            params: list[object] = [json.dumps(updated_cost)]
            if has_metadata_column:
                set_clauses.append("metadata = %s::jsonb")
                params.append(json.dumps({**existing_metadata, "pipeline_run_id": pipeline_run_id}))
            set_clauses.extend(["status = %s", "finished_at = CASE WHEN %s = 'delivery' THEN NOW() ELSE finished_at END"])
            params.extend(["completed" if stage == "delivery" else "running", stage, run_id])
            cursor.execute(
                f"""
                UPDATE pipeline_runs
                SET {', '.join(set_clauses)}
                WHERE id = %s
                """,
                tuple(params),
            )
            return

        if has_metadata_column:
            cursor.execute(
                """
                UPDATE pipeline_runs
                SET metadata = %s::jsonb,
                    status = %s,
                    finished_at = CASE WHEN %s = 'delivery' THEN NOW() ELSE finished_at END
                WHERE id = %s
                """,
                (
                    json.dumps({**existing_metadata, "pipeline_run_id": pipeline_run_id, "cost_estimate_json": updated_cost}),
                    "completed" if stage == "delivery" else "running",
                    stage,
                    run_id,
                ),
            )
            return

        cursor.execute(
            """
            UPDATE pipeline_runs
            SET status = %s,
                finished_at = CASE WHEN %s = 'delivery' THEN NOW() ELSE finished_at END
            WHERE id = %s
            """,
            (
                "completed" if stage == "delivery" else "running",
                stage,
                run_id,
            ),
        )


def _log_event(*, pipeline_run_id: str, stage: str, event: str, elapsed_s: float | None = None, **extra: object) -> None:
    payload: dict[str, object] = {
        "pipeline_run_id": pipeline_run_id,
        "stage": stage,
        "event": event,
    }
    if elapsed_s is not None:
        payload["elapsed_s"] = round(elapsed_s, 3)
    payload.update(extra)
    LOGGER.info(json.dumps(payload, sort_keys=True, default=str))


def _run_stage(stage: str, pipeline_run_id: str) -> None:
    start = time.perf_counter()
    _log_event(pipeline_run_id=pipeline_run_id, stage=stage, event="start")

    # Placeholder for stage-specific implementation.
    _ = load_settings()

    elapsed = time.perf_counter() - start
    _log_event(pipeline_run_id=pipeline_run_id, stage=stage, event="complete", elapsed_s=elapsed)


def _source_metadata(record: object) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for attribute in ("url", "content", "feed_name", "guid", "channel_id", "video_id"):
        value = getattr(record, attribute, None)
        if value not in (None, ""):
            metadata[attribute] = value

    if "feed_name" in metadata:
        metadata["feed"] = metadata.pop("feed_name")

    return metadata


def _insert_sources(connection: object, records: list[object]) -> int:
    if not records:
        return 0

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO sources (source_type, source_key, title, published_at, metadata)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (source_type, source_key) DO NOTHING
            """,
            [
                (
                    record.source_type,
                    record.source_key,
                    record.title,
                    record.published_at,
                    json.dumps(_source_metadata(record)),
                )
                for record in records
            ],
        )
    connection.commit()
    return len(records)


def run_ingestion(*, pipeline_run_id: str | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    start = time.perf_counter()
    _log_event(pipeline_run_id=pipeline_run_id, stage="ingestion", event="start")

    settings = load_settings()
    youtube_channels = load_youtube_channel_configs_from_env()

    # YouTube ingestion runs exclusively through TranscriptAPI.
    youtube_records, failed_channels, missing_transcripts = fetch_all_channels(
        youtube_channels,
        api_key=settings.transcript_api_key,
    )

    # RSS ingestion runs exclusively through NewsBlur.
    newsblur_config = load_newsblur_config_from_env()
    failed_newsblur = 0
    if newsblur_config is not None:
        rss_records, nb_error = fetch_newsblur_records(newsblur_config)
        failed_newsblur = int(nb_error is not None)
    else:
        LOGGER.warning(
            "NewsBlur credentials not configured (NEWSBLUR_USERNAME / NEWSBLUR_PASSWORD); skipping RSS ingestion."
        )
        rss_records = []

    import psycopg

    with psycopg.connect(settings.postgres_dsn) as connection:
        deduped_rss = filter_existing_records(connection, rss_records)
        deduped_youtube = filter_existing_youtube_records(connection, youtube_records)
        inserted = _insert_sources(connection, [*deduped_rss.new_records, *deduped_youtube.new_records])
        _persist_stage_cost_metrics(
            connection,
            pipeline_run_id=pipeline_run_id,
            stage="ingestion",
            metrics={
                "token_count": 0,
                "estimated_cost_usd": 0.0,
                "notes": "Network/data fetch stage; no model token billing.",
            },
        )
        connection.commit()

    elapsed = time.perf_counter() - start
    _log_event(
        pipeline_run_id=pipeline_run_id,
        stage="ingestion",
        event="complete",
        elapsed_s=elapsed,
        fetched_rss=len(rss_records),
        fetched_youtube=len(youtube_records),
        deduped_rss=len(deduped_rss.duplicate_records),
        deduped_youtube=len(deduped_youtube.duplicate_records),
        inserted=inserted,
        failed_newsblur=failed_newsblur,
        failed_youtube=failed_channels,
        missing_youtube_transcripts=missing_transcripts,
    )
    return pipeline_run_id


def run_embedding(
    *,
    pipeline_run_id: str | None = None,
    window_size: int | None = None,
    overlap: int | None = None,
    embedding_batch_size: int | None = None,
    embedding_model_override: str | None = None,
) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())

    start = time.perf_counter()
    _log_event(pipeline_run_id=pipeline_run_id, stage="embedding", event="start")

    settings = load_settings()
    window_size = window_size if window_size is not None else int(os.getenv("CHUNK_WINDOW_SIZE", "200"))
    overlap = overlap if overlap is not None else int(os.getenv("CHUNK_OVERLAP", "40"))
    embedding_batch_size = (
        embedding_batch_size if embedding_batch_size is not None else int(os.getenv("EMBEDDING_BATCH_SIZE", "64"))
    )
    embedding_model_override = embedding_model_override or os.getenv("OPENAI_EMBEDDING_MODEL_OVERRIDE") or None

    import psycopg

    sources_processed = 0
    chunks_created = 0
    chunks_updated = 0
    embeddings_upserted = 0

    with psycopg.connect(settings.postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    s.id,
                    s.metadata ->> 'content' AS content
                FROM sources AS s
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) AS chunk_count,
                        MAX(updated_at) AS latest_chunk_updated_at
                    FROM chunks
                    WHERE source_id = s.id
                ) AS c ON TRUE
                WHERE COALESCE(s.metadata ->> 'content', '') <> ''
                  AND (
                    c.chunk_count = 0
                    OR s.updated_at > COALESCE(c.latest_chunk_updated_at, '-infinity'::timestamptz)
                  )
                ORDER BY s.id
                """
            )
            sources_to_process = cursor.fetchall()

        for source_id, content in sources_to_process:
            if not content:
                continue

            sources_processed += 1
            desired_chunks = chunk_text(content, window_size=window_size, overlap=overlap)

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT chunk_index, content, token_count
                    FROM chunks
                    WHERE source_id = %s
                    """,
                    (source_id,),
                )
                existing_rows = cursor.fetchall()

            existing = {
                chunk_index: (chunk_content, token_count)
                for chunk_index, chunk_content, token_count in existing_rows
            }

            for chunk in desired_chunks:
                existing_chunk = existing.get(chunk.chunk_index)

                if existing_chunk is None:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO chunks (source_id, chunk_index, content, token_count)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (source_id, chunk.chunk_index, chunk.content, chunk.token_count),
                        )
                    chunks_created += 1
                    continue

                existing_content, existing_token_count = existing_chunk
                if existing_content == chunk.content and existing_token_count == chunk.token_count:
                    continue

                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE chunks
                        SET content = %s,
                            token_count = %s
                        WHERE source_id = %s
                          AND chunk_index = %s
                        """,
                        (chunk.content, chunk.token_count, source_id, chunk.chunk_index),
                    )
                chunks_updated += 1

            desired_indexes = {chunk.chunk_index for chunk in desired_chunks}
            stale_indexes = [chunk_index for chunk_index in existing if chunk_index not in desired_indexes]
            if stale_indexes:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM chunks
                        WHERE source_id = %s
                          AND chunk_index = ANY(%s)
                        """,
                        (source_id, stale_indexes),
                    )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.id, c.content, c.token_count
                FROM chunks AS c
                LEFT JOIN embeddings AS e
                    ON e.chunk_id = c.id
                    AND e.model = %s
                WHERE e.id IS NULL
                   OR c.updated_at > e.updated_at
                ORDER BY c.id
                """,
                (embedding_model_override or settings.openai_embedding_model,),
            )
            chunks_to_embed = cursor.fetchall()

        embedding_token_count = 0
        if chunks_to_embed:
            embedding_token_count = sum((token_count or _estimate_tokens_from_text(content)) for _, content, token_count in chunks_to_embed)
            embedded_rows = embed_chunks(
                chunks=[(chunk_id, content) for chunk_id, content, _ in chunks_to_embed],
                settings=settings,
                model=embedding_model_override,
                batch_size=embedding_batch_size,
            )
            embeddings_upserted = upsert_embeddings(connection, embedded_rows)

        _persist_stage_cost_metrics(
            connection,
            pipeline_run_id=pipeline_run_id,
            stage="embedding",
            metrics={
                "token_count": embedding_token_count,
                "estimated_cost_usd": round((embedding_token_count / 1_000_000) * EMBEDDING_COST_PER_MILLION_TOKENS_USD, 6),
                "model": embedding_model_override or settings.openai_embedding_model,
                "cost_per_million_tokens_usd": EMBEDDING_COST_PER_MILLION_TOKENS_USD,
            },
        )

        connection.commit()

    elapsed = time.perf_counter() - start
    _log_event(
        pipeline_run_id=pipeline_run_id,
        stage="embedding",
        event="complete",
        elapsed_s=elapsed,
        sources_processed=sources_processed,
        chunks_created=chunks_created,
        chunks_updated=chunks_updated,
        embeddings_upserted=embeddings_upserted,
        embeddings_token_count=embedding_token_count,
        embedding_model=embedding_model_override or settings.openai_embedding_model,
    )

    return pipeline_run_id


def run_generation(
    *,
    pipeline_run_id: str | None = None,
    topic: str | None = None,
    artifacts_dir: str | None = None,
) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    start = time.perf_counter()
    _log_event(pipeline_run_id=pipeline_run_id, stage="generation", event="start")

    settings = load_settings()
    topic = topic or os.getenv("REPORT_TOPIC") or None
    artifacts_dir_path = Path(artifacts_dir or os.getenv("REPORT_ARTIFACTS_DIR", "artifacts/reports")) / pipeline_run_id
    artifacts_dir_path.mkdir(parents=True, exist_ok=True)

    import psycopg

    with psycopg.connect(settings.postgres_dsn) as connection:
        if topic is None:
            trend_start = time.perf_counter()
            trend_result = run_trend_pass(connection, settings=settings)
            if isinstance(trend_result, TrendPassResult):
                topic = trend_result.topic
            else:
                topic = trend_result
            trend_elapsed = time.perf_counter() - trend_start
            _log_event(
                pipeline_run_id=pipeline_run_id,
                stage="generation",
                event="trend_discovered",
                topic=topic,
                elapsed_s=round(trend_elapsed, 3),
            )

        research_start = time.perf_counter()
        context_packet = run_research_pass(connection, topic=topic, settings=settings)
        research_elapsed = time.perf_counter() - research_start

        draft_start = time.perf_counter()
        draft_markdown = run_draft_pass(topic=topic, context_packet=context_packet, settings=settings)
        draft_elapsed = time.perf_counter() - draft_start

        critique_start = time.perf_counter()
        critique_markdown = run_critique_pass(
            topic=topic,
            context_packet=context_packet,
            draft_markdown=draft_markdown,
            settings=settings,
        )
        critique_elapsed = time.perf_counter() - critique_start

        revision_start = time.perf_counter()
        final_markdown = run_revision_pass(
            topic=topic,
            context_packet=context_packet,
            draft_markdown=draft_markdown,
            critique_markdown=critique_markdown,
            settings=settings,
        )
        revision_elapsed = time.perf_counter() - revision_start

        stage_metrics = {
            "topic": topic,
            "query_count": len(context_packet.queries),
            "context_chunks": len(context_packet.chunks),
            "research_elapsed_s": round(research_elapsed, 3),
            "draft_elapsed_s": round(draft_elapsed, 3),
            "critique_elapsed_s": round(critique_elapsed, 3),
            "revision_elapsed_s": round(revision_elapsed, 3),
        }

        draft_path = artifacts_dir_path / "draft.md"
        critique_path = artifacts_dir_path / "critique.md"
        final_path = artifacts_dir_path / "final.md"
        context_path = artifacts_dir_path / "context_packet.json"

        draft_path.write_text(draft_markdown, encoding="utf-8")
        critique_path.write_text(critique_markdown, encoding="utf-8")
        final_path.write_text(final_markdown, encoding="utf-8")
        context_path.write_text(context_packet.to_json(), encoding="utf-8")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO reports (report_type, title, content, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    "draft",
                    f"Draft: {topic}",
                    draft_markdown,
                    json.dumps({
                        "pipeline_run_id": pipeline_run_id,
                        "stage_metrics": stage_metrics,
                        "artifact_path": str(draft_path),
                    }),
                ),
            )
            cursor.execute(
                """
                INSERT INTO reports (report_type, title, content, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    "final",
                    f"Final: {topic}",
                    final_markdown,
                    json.dumps({
                        "pipeline_run_id": pipeline_run_id,
                        "stage_metrics": stage_metrics,
                        "artifact_path": str(final_path),
                        "critique_artifact_path": str(critique_path),
                        "context_artifact_path": str(context_path),
                    }),
                ),
            )

        generation_model_tier = _resolve_generation_model_tier(settings.anthropic_model_id)
        research_input_tokens = sum(_estimate_tokens_from_text(chunk.text) for chunk in context_packet.chunks)
        draft_input_tokens = _estimate_tokens_from_text(context_packet.to_json()) + _estimate_tokens_from_text(topic)
        draft_output_tokens = _estimate_tokens_from_text(draft_markdown)
        critique_input_tokens = draft_input_tokens + draft_output_tokens
        critique_output_tokens = _estimate_tokens_from_text(critique_markdown)
        revision_input_tokens = critique_input_tokens + critique_output_tokens
        revision_output_tokens = _estimate_tokens_from_text(final_markdown)

        generation_token_count = (
            research_input_tokens
            + draft_input_tokens
            + draft_output_tokens
            + critique_input_tokens
            + critique_output_tokens
            + revision_input_tokens
            + revision_output_tokens
        )
        generation_estimated_cost_usd = round(
            _estimate_generation_step_cost(input_tokens=research_input_tokens, output_tokens=0, model_tier=generation_model_tier)
            + _estimate_generation_step_cost(input_tokens=draft_input_tokens, output_tokens=draft_output_tokens, model_tier=generation_model_tier)
            + _estimate_generation_step_cost(input_tokens=critique_input_tokens, output_tokens=critique_output_tokens, model_tier=generation_model_tier)
            + _estimate_generation_step_cost(input_tokens=revision_input_tokens, output_tokens=revision_output_tokens, model_tier=generation_model_tier),
            6,
        )
        _persist_stage_cost_metrics(
            connection,
            pipeline_run_id=pipeline_run_id,
            stage="generation",
            metrics={
                "token_count": generation_token_count,
                "estimated_cost_usd": generation_estimated_cost_usd,
                "model": settings.anthropic_model_id,
                "model_tier": generation_model_tier,
            },
        )
        connection.commit()

    elapsed = time.perf_counter() - start
    _log_event(
        pipeline_run_id=pipeline_run_id,
        stage="generation",
        event="complete",
        elapsed_s=elapsed,
        **stage_metrics,
        generation_token_count=generation_token_count,
        generation_estimated_cost_usd=generation_estimated_cost_usd,
        draft_artifact=str(draft_path),
        final_artifact=str(final_path),
    )
    return pipeline_run_id


def run_verification(*, pipeline_run_id: str | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    start = time.perf_counter()
    _log_event(pipeline_run_id=pipeline_run_id, stage="verification", event="start")

    settings = load_settings()

    import psycopg

    claims_extracted = 0
    with psycopg.connect(settings.postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, content
                FROM reports
                WHERE report_type = 'final'
                  AND metadata ->> 'pipeline_run_id' = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (pipeline_run_id,),
            )
            report_row = cursor.fetchone()

        if report_row is None:
            elapsed = time.perf_counter() - start
            _log_event(
                pipeline_run_id=pipeline_run_id,
                stage="verification",
                event="complete",
                elapsed_s=elapsed,
                report_found=False,
            )
            return pipeline_run_id

        report_id, report_markdown = report_row
        claims = extract_claims(report_markdown)
        claims_extracted = len(claims)

        cited_chunk_ids = sorted({chunk_id for claim in claims for chunk_id in claim.cited_chunk_ids})
        chunk_text_by_id: dict[int, str] = {}
        source_id_by_chunk_id: dict[int, int | None] = {}

        if cited_chunk_ids:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, source_id, content
                    FROM chunks
                    WHERE id = ANY(%s)
                    """,
                    (cited_chunk_ids,),
                )
                for chunk_id, source_id, content in cursor.fetchall():
                    chunk_text_by_id[chunk_id] = content
                    source_id_by_chunk_id[chunk_id] = source_id

        verification_results = check_claims_against_citations(claims, chunk_text_by_id)
        score = score_claim_results(verification_results)
        result_by_claim_id = {result.claim_id: result for result in verification_results}

        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM claims WHERE report_id = %s", (report_id,))

            claim_rows = []
            for claim in claims:
                result = result_by_claim_id[claim.claim_id]
                primary_chunk_id = claim.cited_chunk_ids[0] if claim.cited_chunk_ids else None
                source_id = source_id_by_chunk_id.get(primary_chunk_id) if primary_chunk_id is not None else None
                claim_rows.append(
                    (
                        report_id,
                        source_id,
                        primary_chunk_id,
                        claim.text,
                        result.score,
                        json.dumps(
                            {
                                "claim_id": claim.claim_id,
                                "cited_chunk_ids": list(claim.cited_chunk_ids),
                                "evaluated_chunk_ids": list(result.evaluated_chunk_ids),
                                "verification_status": result.status,
                                "verification_score": result.score,
                            }
                        ),
                    )
                )

            if claim_rows:
                cursor.executemany(
                    """
                    INSERT INTO claims (report_id, source_id, chunk_id, claim_text, confidence, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    claim_rows,
                )

            cursor.execute(
                """
                UPDATE reports
                SET metadata = jsonb_set(
                    metadata,
                    '{verification}',
                    %s::jsonb,
                    true
                )
                WHERE id = %s
                """,
                (
                    json.dumps(
                        {
                            "total_claims": score.total_claims,
                            "supported_claims": score.supported_claims,
                            "uncertain_claims": score.uncertain_claims,
                            "unsupported_claims": score.unsupported_claims,
                            "quality_score": score.quality_score,
                        }
                    ),
                    report_id,
                ),
            )

        verification_token_count = _estimate_tokens_from_text(report_markdown) + sum(
            _estimate_tokens_from_text(chunk_text) for chunk_text in chunk_text_by_id.values()
        )
        _persist_stage_cost_metrics(
            connection,
            pipeline_run_id=pipeline_run_id,
            stage="verification",
            metrics={
                "token_count": verification_token_count,
                "estimated_cost_usd": 0.0,
                "notes": "Rule-based verification currently has no external model billing.",
            },
        )

        connection.commit()

    elapsed = time.perf_counter() - start
    _log_event(
        pipeline_run_id=pipeline_run_id,
        stage="verification",
        event="complete",
        elapsed_s=elapsed,
        report_found=True,
        claims_extracted=claims_extracted,
        quality_score=score.quality_score,
        supported_claims=score.supported_claims,
        uncertain_claims=score.uncertain_claims,
        unsupported_claims=score.unsupported_claims,
    )
    return pipeline_run_id


def _build_delivery_summary(*, title: str, verification_metadata: dict[str, object]) -> str:
    quality_score = verification_metadata.get("quality_score")
    total_claims = verification_metadata.get("total_claims")
    supported_claims = verification_metadata.get("supported_claims")

    summary_parts = [f"{title}"]
    if quality_score is not None:
        summary_parts.append(f"quality score: {quality_score}")
    if total_claims is not None and supported_claims is not None:
        summary_parts.append(f"supported claims: {supported_claims}/{total_claims}")
    return " | ".join(summary_parts)


def run_delivery(*, pipeline_run_id: str | None = None, dry_run: bool | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    start = time.perf_counter()
    _log_event(pipeline_run_id=pipeline_run_id, stage="delivery", event="start")

    postgres_dsn = os.getenv("POSTGRES_DSN", "")
    if postgres_dsn == "":
        raise ValueError("POSTGRES_DSN must be configured for delivery stage")

    github_token = os.getenv("GITHUB_TOKEN", "")
    github_owner = os.getenv("GITHUB_OWNER", "")
    github_repo = os.getenv("GITHUB_REPO", "")
    if not github_owner or not github_repo:
        _repo_full = os.getenv("GITHUB_REPOSITORY", "")
        if "/" in _repo_full:
            _derived_owner, _derived_repo = _repo_full.split("/", 1)
            github_owner = github_owner or _derived_owner
            github_repo = github_repo or _derived_repo
    github_branch = os.getenv("GITHUB_DEFAULT_BRANCH", "main")
    dry_run = dry_run if dry_run is not None else os.getenv("DELIVERY_DRY_RUN", "false").lower() == "true"

    if github_token == "" or github_owner == "" or github_repo == "":
        raise ValueError("GITHUB_TOKEN and GITHUB_REPOSITORY (or GITHUB_OWNER + GITHUB_REPO) are required for delivery stage")

    import psycopg

    with psycopg.connect(postgres_dsn) as connection:
        with connection.cursor() as cursor:
            if pipeline_run_id:
                cursor.execute(
                    """
                    SELECT id, title, content, metadata, created_at
                    FROM reports
                    WHERE report_type = 'final'
                      AND metadata ->> 'pipeline_run_id' = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (pipeline_run_id,),
                )
                report_row = cursor.fetchone()
            else:
                report_row = None

            if report_row is None:
                cursor.execute(
                    """
                    SELECT id, title, content, metadata, created_at
                    FROM reports
                    WHERE report_type = 'final'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                report_row = cursor.fetchone()

    if report_row is None:
        with psycopg.connect(postgres_dsn) as connection:
            _persist_stage_cost_metrics(
                connection,
                pipeline_run_id=pipeline_run_id,
                stage="delivery",
                metrics={
                    "token_count": 0,
                    "estimated_cost_usd": 0.0,
                    "report_found": False,
                },
            )
            connection.commit()

        elapsed = time.perf_counter() - start
        _log_event(
            pipeline_run_id=pipeline_run_id,
            stage="delivery",
            event="complete",
            elapsed_s=elapsed,
            report_found=False,
        )
        return pipeline_run_id

    _, title, markdown_content, metadata, created_at = report_row
    report_metadata = metadata if isinstance(metadata, dict) else {}
    verification_metadata = report_metadata.get("verification", {})
    if not isinstance(verification_metadata, dict):
        verification_metadata = {}

    summary = _build_delivery_summary(title=title, verification_metadata=verification_metadata)

    if isinstance(created_at, datetime):
        created_at_dt = created_at
    else:
        created_at_dt = datetime.now()

    github_result = publish_report_markdown(
        report_markdown=markdown_content,
        report_title=title,
        report_created_at=created_at_dt,
        github_token=github_token,
        github_owner=github_owner,
        github_repo=github_repo,
        github_branch=github_branch,
        dry_run=dry_run,
    )

    email_enabled = os.getenv("DELIVERY_EMAIL_ENABLED", "false").lower() == "true"
    slack_enabled = os.getenv("DELIVERY_SLACK_ENABLED", "false").lower() == "true"

    email_sent = False
    if email_enabled:
        email_result = send_summary_email(
            report_title=title,
            summary=summary,
            report_url=github_result.html_url,
            report_markdown=markdown_content,
            dry_run=dry_run,
        )
        email_sent = email_result.delivered

    slack_sent = False
    if slack_enabled:
        slack_result = post_report_summary(summary=summary, report_url=github_result.html_url, dry_run=dry_run)
        slack_sent = slack_result.delivered

    with psycopg.connect(postgres_dsn) as connection:
        _persist_stage_cost_metrics(
            connection,
            pipeline_run_id=pipeline_run_id,
            stage="delivery",
            metrics={
                "token_count": 0,
                "estimated_cost_usd": 0.0,
                "report_found": True,
                "dry_run": dry_run,
            },
        )
        connection.commit()

    elapsed = time.perf_counter() - start
    _log_event(
        pipeline_run_id=pipeline_run_id,
        stage="delivery",
        event="complete",
        elapsed_s=elapsed,
        report_found=True,
        report_title=title,
        github_path=github_result.path,
        github_url=github_result.html_url,
        github_committed=github_result.committed,
        dry_run=dry_run,
        email_enabled=email_enabled,
        email_sent=email_sent,
        slack_enabled=slack_enabled,
        slack_sent=slack_sent,
    )
    return pipeline_run_id


def _backfill_markdown_connection(connection: object, *, batch_size: int = 100) -> tuple[int, int]:
    """Convert HTML in sources.metadata['content'] to markdown for one connection.

    Returns ``(converted, skipped)`` counts.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, metadata
            FROM sources
            WHERE metadata ->> 'content' LIKE '%<%'
            ORDER BY id
            """
        )
        rows = cursor.fetchall()

    converted = 0
    skipped = 0

    for source_id, metadata in rows:
        content = metadata.get("content", "") if isinstance(metadata, dict) else ""
        if not content or "<" not in content:
            skipped += 1
            continue

        markdown = html_to_markdown(content)
        if markdown == content:
            skipped += 1
            continue

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sources
                SET metadata = jsonb_set(metadata, '{content}', %s::jsonb)
                WHERE id = %s
                """,
                (json.dumps(markdown), source_id),
            )
        converted += 1

        if converted % batch_size == 0:
            connection.commit()
            LOGGER.info("backfill-markdown: converted %d sources so far", converted)

    connection.commit()
    return converted, skipped


def run_backfill_markdown(*, pipeline_run_id: str | None = None, batch_size: int = 100) -> str:
    """Convert existing HTML content in sources to markdown in-place.

    Finds all sources whose ``metadata->>'content'`` contains HTML (detected by
    the presence of ``<``), converts it with :func:`html_to_markdown`, and
    writes the result back into ``sources.metadata``.

    The ``updated_at`` trigger fires on each UPDATE, so the next run of the
    embedding stage will automatically re-chunk and re-embed those sources.
    """
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    start = time.perf_counter()
    _log_event(pipeline_run_id=pipeline_run_id, stage="backfill-markdown", event="start")

    settings = load_settings()

    import psycopg

    with psycopg.connect(settings.postgres_dsn) as connection:
        converted, skipped = _backfill_markdown_connection(connection, batch_size=batch_size)

    elapsed = time.perf_counter() - start
    _log_event(
        pipeline_run_id=pipeline_run_id,
        stage="backfill-markdown",
        event="complete",
        elapsed_s=elapsed,
        sources_converted=converted,
        sources_skipped=skipped,
    )
    return pipeline_run_id


def run_all(*, pipeline_run_id: str | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    for stage in ("ingestion", "embedding", "generation", "verification", "delivery"):
        _run_stage(stage, pipeline_run_id)
    return pipeline_run_id


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run report pipeline stages")
    parser.add_argument("--pipeline-run-id", default=None, help="Run identifier shared across stages")

    subparsers = parser.add_subparsers(dest="stage", required=True)

    subparsers.add_parser("ingestion", help="Run ingestion stage")

    embedding_parser = subparsers.add_parser("embedding", help="Run embedding stage")
    embedding_parser.add_argument("--chunk-window-size", type=int, default=None)
    embedding_parser.add_argument("--chunk-overlap", type=int, default=None)
    embedding_parser.add_argument("--embedding-batch-size", type=int, default=None)
    embedding_parser.add_argument("--embedding-model", default=None)

    generation_parser = subparsers.add_parser("generation", help="Run generation stage")
    generation_parser.add_argument("--topic", default=None)
    generation_parser.add_argument("--artifacts-dir", default=None)

    subparsers.add_parser("verification", help="Run verification stage")

    delivery_parser = subparsers.add_parser("delivery", help="Run delivery stage")
    delivery_parser.add_argument("--dry-run", action="store_true", help="Publish outputs in dry-run mode")

    subparsers.add_parser("all", help="Run all stages")

    backfill_parser = subparsers.add_parser(
        "backfill-markdown",
        help="Convert existing HTML content in sources to markdown (one-time migration)",
    )
    backfill_parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Commit after every N converted rows (default: 100)",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.stage == "ingestion":
        pipeline_run_id = run_ingestion(pipeline_run_id=args.pipeline_run_id)
    elif args.stage == "embedding":
        pipeline_run_id = run_embedding(
            pipeline_run_id=args.pipeline_run_id,
            window_size=args.chunk_window_size,
            overlap=args.chunk_overlap,
            embedding_batch_size=args.embedding_batch_size,
            embedding_model_override=args.embedding_model,
        )
    elif args.stage == "generation":
        pipeline_run_id = run_generation(
            pipeline_run_id=args.pipeline_run_id,
            topic=args.topic,
            artifacts_dir=args.artifacts_dir,
        )
    elif args.stage == "verification":
        pipeline_run_id = run_verification(pipeline_run_id=args.pipeline_run_id)
    elif args.stage == "delivery":
        pipeline_run_id = run_delivery(pipeline_run_id=args.pipeline_run_id, dry_run=args.dry_run)
    elif args.stage == "backfill-markdown":
        pipeline_run_id = run_backfill_markdown(
            pipeline_run_id=args.pipeline_run_id,
            batch_size=args.batch_size,
        )
    else:
        pipeline_run_id = run_all(pipeline_run_id=args.pipeline_run_id)

    print(pipeline_run_id)


if __name__ == "__main__":
    main()
