"""Pipeline entrypoints and stage orchestration."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

from .config import load_settings
from .ingestion.dedupe import filter_existing_records, filter_existing_youtube_records
from .ingestion.rss import fetch_all_feeds, load_feed_configs_from_env
from .ingestion.youtube import fetch_all_channels, load_youtube_channel_configs_from_env
from .processing.chunking import chunk_text
from .processing.embeddings import embed_chunks, upsert_embeddings

LOGGER = logging.getLogger("research.pipeline")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


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
    feed_configs = load_feed_configs_from_env()
    youtube_channels = load_youtube_channel_configs_from_env()

    rss_records, failed_feeds = fetch_all_feeds(feed_configs)
    youtube_records, failed_channels, missing_transcripts = fetch_all_channels(
        youtube_channels,
        api_key=settings.transcript_api_key,
    )

    import psycopg

    with psycopg.connect(settings.postgres_dsn) as connection:
        deduped_rss = filter_existing_records(connection, rss_records)
        deduped_youtube = filter_existing_youtube_records(connection, youtube_records)
        inserted = _insert_sources(connection, [*deduped_rss.new_records, *deduped_youtube.new_records])

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
        failed_rss=failed_feeds,
        failed_youtube=failed_channels,
        missing_youtube_transcripts=missing_transcripts,
    )
    return pipeline_run_id


def run_embedding(*, pipeline_run_id: str | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())

    start = time.perf_counter()
    _log_event(pipeline_run_id=pipeline_run_id, stage="embedding", event="start")

    settings = load_settings()
    window_size = int(os.getenv("CHUNK_WINDOW_SIZE", "200"))
    overlap = int(os.getenv("CHUNK_OVERLAP", "40"))
    embedding_batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "64"))
    embedding_model_override = os.getenv("OPENAI_EMBEDDING_MODEL_OVERRIDE") or None

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
                SELECT c.id, c.content
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

        if chunks_to_embed:
            embedded_rows = embed_chunks(
                chunks=chunks_to_embed,
                settings=settings,
                model=embedding_model_override,
                batch_size=embedding_batch_size,
            )
            embeddings_upserted = upsert_embeddings(connection, embedded_rows)

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
        embedding_model=embedding_model_override or settings.openai_embedding_model,
    )

    return pipeline_run_id


def run_generation(*, pipeline_run_id: str | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    _run_stage("generation", pipeline_run_id)
    return pipeline_run_id


def run_verification(*, pipeline_run_id: str | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    _run_stage("verification", pipeline_run_id)
    return pipeline_run_id


def run_delivery(*, pipeline_run_id: str | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    _run_stage("delivery", pipeline_run_id)
    return pipeline_run_id


def run_all(*, pipeline_run_id: str | None = None) -> str:
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    for stage in ("ingestion", "embedding", "generation", "verification", "delivery"):
        _run_stage(stage, pipeline_run_id)
    return pipeline_run_id
