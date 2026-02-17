"""RSS ingestion utilities with retry/backoff and deterministic record output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import logging
import os
import time
from typing import Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

LOGGER = logging.getLogger("research.ingestion.rss")


@dataclass(frozen=True)
class FeedConfig:
    """Configuration for a single RSS or Atom feed."""

    name: str
    url: str
    timeout_s: float = 10.0
    retries: int = 2
    backoff_base_s: float = 0.5


@dataclass(frozen=True)
class RSSRecord:
    """Normalized record extracted from a feed item."""

    source_type: str
    source_key: str
    title: str
    url: str
    published_at: datetime | None
    content: str
    feed_name: str
    guid: str | None


RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _parse_datetime(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    for parser in (
        lambda value: parsedate_to_datetime(value),
        lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
    ):
        try:
            dt = parser(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except (TypeError, ValueError):
            continue
    return None


def _stable_source_key(*, guid: str | None, url: str, title: str, published_at: datetime | None, feed_name: str) -> str:
    if guid:
        return f"guid:{guid.strip()}"
    if url:
        return f"url:{url.strip()}"
    digest_input = "|".join((feed_name.strip(), title.strip(), published_at.isoformat() if published_at else ""))
    return f"hash:{hashlib.sha256(digest_input.encode('utf-8')).hexdigest()}"


def _fetch_feed_document(config: FeedConfig) -> bytes:
    last_error: Exception | None = None
    for attempt in range(config.retries + 1):
        try:
            request = Request(config.url, headers={"User-Agent": "research-pipeline/1.0"})
            with urlopen(request, timeout=config.timeout_s) as response:  # noqa: S310
                return response.read()
        except Exception as exc:  # broad by design for network and parser errors
            last_error = exc
            if attempt >= config.retries:
                break
            time.sleep(config.backoff_base_s * (2**attempt))
    assert last_error is not None
    raise last_error


def _parse_atom_items(root: ET.Element, feed_name: str) -> list[RSSRecord]:
    records: list[RSSRecord] = []
    for entry in root.findall("atom:entry", RSS_NS):
        title = _text(entry.find("atom:title", RSS_NS))
        link = entry.find("atom:link[@rel='alternate']", RSS_NS) or entry.find("atom:link", RSS_NS)
        url = (link.attrib.get("href", "") if link is not None else "").strip()
        guid = _text(entry.find("atom:id", RSS_NS)) or None
        published_at = _parse_datetime(_text(entry.find("atom:published", RSS_NS)) or _text(entry.find("atom:updated", RSS_NS)))
        content = _text(entry.find("atom:content", RSS_NS)) or _text(entry.find("atom:summary", RSS_NS))
        source_key = _stable_source_key(guid=guid, url=url, title=title, published_at=published_at, feed_name=feed_name)
        records.append(
            RSSRecord(
                source_type="rss",
                source_key=source_key,
                title=title,
                url=url,
                published_at=published_at,
                content=content,
                feed_name=feed_name,
                guid=guid,
            )
        )
    return records


def _parse_rss_items(root: ET.Element, feed_name: str) -> list[RSSRecord]:
    records: list[RSSRecord] = []
    for item in root.findall("./channel/item"):
        title = _text(item.find("title"))
        url = _text(item.find("link"))
        guid = _text(item.find("guid")) or None
        published_at = _parse_datetime(_text(item.find("pubDate")))
        content = _text(item.find("content:encoded", RSS_NS)) or _text(item.find("description"))
        source_key = _stable_source_key(guid=guid, url=url, title=title, published_at=published_at, feed_name=feed_name)
        records.append(
            RSSRecord(
                source_type="rss",
                source_key=source_key,
                title=title,
                url=url,
                published_at=published_at,
                content=content,
                feed_name=feed_name,
                guid=guid,
            )
        )
    return records


def parse_feed(content: bytes, *, feed_name: str) -> list[RSSRecord]:
    """Parse a feed payload into normalized deterministic records."""

    root = ET.fromstring(content)
    tag = root.tag.lower()

    if tag.endswith("feed"):
        records = _parse_atom_items(root, feed_name)
    else:
        records = _parse_rss_items(root, feed_name)

    return sorted(records, key=lambda record: (record.source_key, record.url, record.title))


def fetch_feed(config: FeedConfig) -> tuple[list[RSSRecord], Exception | None]:
    """Fetch and parse one feed, returning records and optional error."""

    try:
        payload = _fetch_feed_document(config)
        return parse_feed(payload, feed_name=config.name), None
    except Exception as exc:  # pragma: no cover - defensive logging path
        LOGGER.warning("Failed feed '%s': %s", config.name, exc)
        return [], exc


def fetch_all_feeds(feed_configs: Iterable[FeedConfig]) -> tuple[list[RSSRecord], int]:
    """Fetch all feeds and return records + failed feed count."""

    records: list[RSSRecord] = []
    failed = 0
    for feed_config in feed_configs:
        feed_records, error = fetch_feed(feed_config)
        failed += int(error is not None)
        records.extend(feed_records)
    return sorted(records, key=lambda record: (record.source_key, record.url, record.title)), failed


def load_feed_configs_from_env() -> list[FeedConfig]:
    """Load feed configs from RSS_FEEDS env var (`name|url` comma-separated)."""

    raw = os.getenv("RSS_FEEDS", "")
    timeout = float(os.getenv("RSS_FEED_TIMEOUT_S", "10"))
    retries = int(os.getenv("RSS_FEED_RETRIES", "2"))
    backoff = float(os.getenv("RSS_FEED_BACKOFF_BASE_S", "0.5"))

    configs: list[FeedConfig] = []
    for chunk in [piece.strip() for piece in raw.split(",") if piece.strip()]:
        if "|" in chunk:
            name, url = [value.strip() for value in chunk.split("|", maxsplit=1)]
        else:
            url = chunk
            parsed = urlparse(url)
            name = parsed.netloc or url
        configs.append(FeedConfig(name=name, url=url, timeout_s=timeout, retries=retries, backoff_base_s=backoff))

    return sorted(configs, key=lambda config: (config.name, config.url))
