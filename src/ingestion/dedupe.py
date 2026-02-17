"""Deduplication helpers for ingestion records."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .rss import RSSRecord
from .youtube import YouTubeRecord


@dataclass(frozen=True)
class DedupedRecords:
    """Records split by insertion candidacy."""

    new_records: list[RSSRecord]
    duplicate_records: list[RSSRecord]


@dataclass(frozen=True)
class DedupedYouTubeRecords:
    """YouTube records split by insertion candidacy."""

    new_records: list[YouTubeRecord]
    duplicate_records: list[YouTubeRecord]


def normalize_url(url: str) -> str:
    """Normalize URLs for deduplication checks."""

    raw = url.strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    if path == "":
        path = "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))

    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def canonical_source_key(record: RSSRecord) -> str:
    """Canonical key used to compare feed records."""

    if record.guid:
        return f"guid:{record.guid.strip()}"
    return record.source_key.strip()


def dedupe_records(records: Iterable[RSSRecord]) -> DedupedRecords:
    """Drop duplicates within a batch based on GUID/source key and normalized URL."""

    seen_keys: set[str] = set()
    seen_urls: set[str] = set()
    new_records: list[RSSRecord] = []
    dup_records: list[RSSRecord] = []

    for record in sorted(records, key=lambda item: (item.source_key, item.url, item.title)):
        key = canonical_source_key(record)
        normalized_url = normalize_url(record.url)
        if key in seen_keys or (normalized_url and normalized_url in seen_urls):
            dup_records.append(record)
            continue

        seen_keys.add(key)
        if normalized_url:
            seen_urls.add(normalized_url)
        new_records.append(record)

    return DedupedRecords(new_records=new_records, duplicate_records=dup_records)


def filter_existing_records(connection: object, records: Sequence[RSSRecord]) -> DedupedRecords:
    """Filter out records that already exist in `sources` based on key or normalized URL."""

    if not records:
        return DedupedRecords(new_records=[], duplicate_records=[])

    in_batch = dedupe_records(records)
    if not in_batch.new_records:
        return in_batch

    source_keys = [record.source_key for record in in_batch.new_records]
    urls = [record.url for record in in_batch.new_records if record.url]

    existing_source_keys: set[str] = set()
    existing_urls: set[str] = set()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_key, metadata->>'url' AS source_url
            FROM sources
            WHERE source_type = %s
              AND (source_key = ANY(%s) OR metadata->>'url' = ANY(%s))
            """,
            ("rss", source_keys, urls or [""],),
        )
        for source_key, source_url in cursor.fetchall():
            existing_source_keys.add(str(source_key))
            if source_url:
                existing_urls.add(normalize_url(str(source_url)))

    new_records: list[RSSRecord] = []
    duplicate_records = list(in_batch.duplicate_records)
    for record in in_batch.new_records:
        normalized_url = normalize_url(record.url)
        if record.source_key in existing_source_keys or (normalized_url and normalized_url in existing_urls):
            duplicate_records.append(record)
        else:
            new_records.append(record)

    return DedupedRecords(new_records=new_records, duplicate_records=duplicate_records)


def canonical_channel_video_key(record: YouTubeRecord) -> str:
    """Canonical key for YouTube records using channel/video identity."""

    return f"{record.channel_id.strip().lower()}:{record.video_id.strip()}"


def dedupe_youtube_records(records: Iterable[YouTubeRecord]) -> DedupedYouTubeRecords:
    """Drop YouTube duplicates within a batch using channel/video id and normalized URL."""

    seen_keys: set[str] = set()
    seen_source_keys: set[str] = set()
    seen_urls: set[str] = set()
    new_records: list[YouTubeRecord] = []
    duplicate_records: list[YouTubeRecord] = []

    for record in sorted(records, key=lambda item: (item.channel_id, item.video_id, item.source_key)):
        channel_video_key = canonical_channel_video_key(record)
        normalized_url = normalize_url(record.url)
        if channel_video_key in seen_keys or record.source_key in seen_source_keys or (
            normalized_url and normalized_url in seen_urls
        ):
            duplicate_records.append(record)
            continue

        seen_keys.add(channel_video_key)
        seen_source_keys.add(record.source_key)
        if normalized_url:
            seen_urls.add(normalized_url)
        new_records.append(record)

    return DedupedYouTubeRecords(new_records=new_records, duplicate_records=duplicate_records)


def filter_existing_youtube_records(
    connection: object,
    records: Sequence[YouTubeRecord],
) -> DedupedYouTubeRecords:
    """Filter out YouTube records that already exist by source key, channel/video id, or URL."""

    if not records:
        return DedupedYouTubeRecords(new_records=[], duplicate_records=[])

    in_batch = dedupe_youtube_records(records)
    if not in_batch.new_records:
        return in_batch

    source_keys = [record.source_key for record in in_batch.new_records]
    video_ids = [record.video_id for record in in_batch.new_records if record.video_id]
    urls = [record.url for record in in_batch.new_records if record.url]

    existing_source_keys: set[str] = set()
    existing_channel_video_keys: set[str] = set()
    existing_urls: set[str] = set()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_key,
                   metadata->>'url' AS source_url,
                   metadata->>'channel_id' AS channel_id,
                   metadata->>'video_id' AS video_id
            FROM sources
            WHERE source_type = %s
              AND (
                source_key = ANY(%s)
                OR metadata->>'video_id' = ANY(%s)
                OR metadata->>'url' = ANY(%s)
              )
            """,
            ("youtube", source_keys, video_ids or [""], urls or [""],),
        )
        for source_key, source_url, channel_id, video_id in cursor.fetchall():
            existing_source_keys.add(str(source_key))
            if source_url:
                existing_urls.add(normalize_url(str(source_url)))
            if channel_id and video_id:
                existing_channel_video_keys.add(f"{str(channel_id).strip().lower()}:{str(video_id).strip()}")

    new_records: list[YouTubeRecord] = []
    duplicate_records = list(in_batch.duplicate_records)

    for record in in_batch.new_records:
        normalized_url = normalize_url(record.url)
        channel_video_key = canonical_channel_video_key(record)

        if (
            record.source_key in existing_source_keys
            or channel_video_key in existing_channel_video_keys
            or (normalized_url and normalized_url in existing_urls)
        ):
            duplicate_records.append(record)
        else:
            new_records.append(record)

    return DedupedYouTubeRecords(new_records=new_records, duplicate_records=duplicate_records)
