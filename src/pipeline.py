"""Pipeline entrypoints and stage orchestration."""

from __future__ import annotations

import json
import logging
import time
import uuid

from .config import load_settings
from .ingestion.dedupe import filter_existing_records, filter_existing_youtube_records
from .ingestion.rss import fetch_all_feeds, load_feed_configs_from_env
from .ingestion.youtube import fetch_all_channels, load_youtube_channel_configs_from_env

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
    _run_stage("embedding", pipeline_run_id)
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
