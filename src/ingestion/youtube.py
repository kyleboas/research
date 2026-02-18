"""YouTube ingestion via transcript provider APIs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from socket import gaierror
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

LOGGER = logging.getLogger("research.ingestion.youtube")


@dataclass(frozen=True)
class YouTubeChannelConfig:
    """Configuration for polling a channel's latest videos."""

    name: str
    channel_id: str
    latest_limit: int = 10
    timeout_s: float = 12.0
    retries: int = 2
    backoff_base_s: float = 1.0


@dataclass(frozen=True)
class YouTubeRecord:
    """Normalized YouTube record persisted in `sources`."""

    source_type: str
    source_key: str
    title: str
    url: str
    published_at: datetime | None
    content: str
    feed_name: str
    guid: str
    channel_id: str
    video_id: str


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stable_source_key(*, channel_id: str, video_id: str, title: str, published_at: datetime | None) -> str:
    if channel_id and video_id:
        return f"youtube:{channel_id}:{video_id}"

    digest = hashlib.sha256(
        f"{channel_id}|{video_id}|{title}|{published_at.isoformat() if published_at else ''}".encode("utf-8")
    ).hexdigest()
    return f"youtube:sha256:{digest}"


def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-API-Key": api_key,
    }


def _http_json(
    *,
    base_url: str,
    path: str,
    query: dict[str, str | int],
    api_key: str,
    timeout_s: float,
    retries: int = 2,
    backoff_base_s: float = 1.0,
) -> dict[str, object]:
    encoded_query = urlencode([(key, str(value)) for key, value in query.items() if value != ""])
    url = f"{base_url.rstrip('/')}{path}"
    if encoded_query:
        url = f"{url}?{encoded_query}"

    request = Request(url, headers=_build_headers(api_key), method="GET")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout_s) as response:
                payload = response.read()
            break
        except HTTPError as exc:
            # 4xx errors are permanent — do not retry.
            raise RuntimeError(f"request failed: {exc}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff_base_s * (2**attempt))
    else:
        assert last_error is not None
        raise RuntimeError(f"request failed: {last_error}") from last_error

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid JSON response") from exc

    if not isinstance(decoded, dict):
        raise RuntimeError("unexpected JSON response shape")
    return decoded


def _extract_items(payload: dict[str, object]) -> list[dict[str, object]]:
    for key in ("videos", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _extract_video_id_from_url(url: str) -> str:
    match = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", url)
    return match.group(1) if match else ""


def _fetch_channel_videos_from_rss(channel: YouTubeChannelConfig) -> list[dict[str, object]]:
    """Fetch latest channel videos from the public YouTube RSS feed."""

    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel.channel_id}"
    request = Request(
        feed_url,
        headers={
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            "User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)",
        },
        method="GET",
    )

    last_error: Exception | None = None
    payload = b""
    for attempt in range(channel.retries + 1):
        try:
            with urlopen(request, timeout=channel.timeout_s) as response:
                payload = response.read()
            break
        except Exception as exc:  # pragma: no cover - defensive network handling
            last_error = exc
            if attempt < channel.retries:
                time.sleep(channel.backoff_base_s * (2**attempt))
    else:
        assert last_error is not None
        raise RuntimeError(f"request failed: {last_error}") from last_error

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError("invalid YouTube channel feed XML") from exc

    namespace = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    videos: list[dict[str, object]] = []

    for entry in root.findall("atom:entry", namespace):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=namespace) or "").strip()
        title = (entry.findtext("atom:title", default="", namespaces=namespace) or "").strip()
        url = ""
        link = entry.find("atom:link", namespace)
        if link is not None:
            url = str(link.attrib.get("href", "")).strip()

        if not video_id:
            video_id = _extract_video_id_from_url(url)

        published_at = (entry.findtext("atom:published", default="", namespaces=namespace) or "").strip()
        if not video_id:
            continue

        videos.append(
            {
                "video_id": video_id,
                "title": title,
                "url": url,
                "published_at": published_at,
            }
        )

    return videos[: max(channel.latest_limit, 1)]


def _extract_text(payload: dict[str, object]) -> str:
    for key in ("transcript", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            chunks = [piece.get("text", "") for piece in value if isinstance(piece, dict)]
            return " ".join(part.strip() for part in chunks if part).strip()
    return ""


def load_youtube_channel_configs_from_env() -> list[YouTubeChannelConfig]:
    """Load channel poll configuration from markdown (`feeds/youtube.md`) or env.

    If ``YOUTUBE_CHANNELS`` is present, that value takes precedence for backwards compatibility.
    """

    raw = os.getenv("YOUTUBE_CHANNELS", "")
    latest_limit = int(os.getenv("YOUTUBE_LATEST_LIMIT", "10"))
    timeout_s = float(os.getenv("YOUTUBE_TIMEOUT_S", "12"))
    retries = int(os.getenv("YOUTUBE_RETRIES", "2"))
    backoff_base_s = float(os.getenv("YOUTUBE_BACKOFF_BASE_S", "1.0"))

    if not raw.strip():
        channels_file = Path(
            os.getenv("YOUTUBE_CHANNELS_FILE", Path(__file__).resolve().parents[2] / "feeds" / "youtube.md")
        )
        raw = _load_channel_configs_csv_from_markdown(channels_file)

    configs: list[YouTubeChannelConfig] = []
    for chunk in [piece.strip() for piece in raw.split(",") if piece.strip()]:
        if "|" in chunk:
            name, channel_id = [part.strip() for part in chunk.split("|", maxsplit=1)]
        else:
            channel_id = chunk
            name = chunk

        if not channel_id:
            continue

        configs.append(
            YouTubeChannelConfig(
                name=name or channel_id,
                channel_id=channel_id,
                latest_limit=max(latest_limit, 1),
                timeout_s=max(timeout_s, 1.0),
                retries=max(retries, 0),
                backoff_base_s=max(backoff_base_s, 0.0),
            )
        )

    return sorted(configs, key=lambda config: (config.name, config.channel_id))


def _load_channel_configs_csv_from_markdown(path: Path) -> str:
    """Convert YouTube channel entries from markdown into `name|channel_id` CSV chunks."""

    if not path.exists():
        LOGGER.warning("YouTube channels markdown file not found: %s", path)
        return ""

    lines = path.read_text(encoding="utf-8").splitlines()
    name_pattern = re.compile(r"^-\s+\*\*(.+?)\*\*\s*$")
    channel_pattern = re.compile(r"^\s*-\s+Channel ID:\s*(\S+)\s*$")

    current_name: str | None = None
    chunks: list[str] = []

    for line in lines:
        name_match = name_pattern.match(line)
        if name_match:
            current_name = name_match.group(1).strip()
            continue

        channel_match = channel_pattern.match(line)
        if channel_match and current_name:
            chunks.append(f"{current_name}|{channel_match.group(1).strip()}")
            current_name = None

    return ",".join(chunks)


def _to_record(video: dict[str, object], *, channel: YouTubeChannelConfig, transcript: str) -> YouTubeRecord:
    video_id = str(video.get("video_id") or video.get("id") or "").strip()
    title = str(video.get("title") or "").strip() or f"YouTube video {video_id}"
    url = str(video.get("url") or video.get("video_url") or "").strip()
    if not url and video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"

    published_at = _parse_datetime(str(video.get("published_at") or video.get("publishedAt") or ""))

    return YouTubeRecord(
        source_type="youtube",
        source_key=_stable_source_key(
            channel_id=channel.channel_id,
            video_id=video_id,
            title=title,
            published_at=published_at,
        ),
        title=title,
        url=url,
        published_at=published_at,
        content=transcript,
        feed_name=channel.name,
        guid=video_id,
        channel_id=channel.channel_id,
        video_id=video_id,
    )


def fetch_channel_latest_videos(
    channel: YouTubeChannelConfig,
    *,
    api_key: str,
    provider_base_url: str,
) -> list[dict[str, object]]:
    """Fetch latest videos for a configured YouTube channel.

    Uses the transcript provider `/youtube/channel/latest` endpoint directly.
    """

    payload = _http_json(
        base_url=provider_base_url,
        path="/youtube/channel/latest",
        query={
            "channel_url": channel.channel_id,
            "limit": channel.latest_limit,
        },
        api_key=api_key,
        timeout_s=channel.timeout_s,
        retries=channel.retries,
        backoff_base_s=channel.backoff_base_s,
    )

    videos: list[dict[str, object]] = []
    for item in _extract_items(payload):
        video_id = str(item.get("video_id") or item.get("id") or "").strip()
        url = str(item.get("url") or item.get("video_url") or "").strip()
        if not video_id and url:
            video_id = _extract_video_id_from_url(url)
        if not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"
        if not video_id:
            continue

        videos.append(
            {
                "video_id": video_id,
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "published_at": str(item.get("published_at") or item.get("publishedAt") or "").strip(),
            }
        )

    return videos[: max(channel.latest_limit, 1)]


def fetch_video_transcript(
    *,
    video_id: str,
    api_key: str,
    provider_base_url: str,
    timeout_s: float,
    retries: int = 2,
    backoff_base_s: float = 1.0,
) -> str:
    """Fetch transcript text for a single YouTube video."""

    payload = _http_json(
        base_url=provider_base_url,
        path="/youtube/transcript",
        query={"video_url": f"https://www.youtube.com/watch?v={video_id}"},
        api_key=api_key,
        timeout_s=timeout_s,
        retries=retries,
        backoff_base_s=backoff_base_s,
    )
    return _extract_text(payload)


def poll_channel_videos(
    channel: YouTubeChannelConfig,
    *,
    api_key: str,
    provider_base_url: str,
) -> tuple[list[YouTubeRecord], int, Exception | None]:
    """Poll latest videos and fetch transcripts; returns records + missing transcript count."""

    try:
        videos = fetch_channel_latest_videos(channel, api_key=api_key, provider_base_url=provider_base_url)
    except Exception as exc:  # pragma: no cover - defensive path
        LOGGER.warning("Failed YouTube channel '%s': %s", channel.name, exc)
        return [], 0, exc

    records: list[YouTubeRecord] = []
    missing_transcripts = 0

    for video in videos:
        video_id = str(video.get("video_id") or video.get("id") or "").strip()
        if not video_id:
            continue

        transcript = ""
        try:
            transcript = fetch_video_transcript(
                video_id=video_id,
                api_key=api_key,
                provider_base_url=provider_base_url,
                timeout_s=channel.timeout_s,
                retries=channel.retries,
                backoff_base_s=channel.backoff_base_s,
            )
        except Exception as exc:  # pragma: no cover - defensive path
            LOGGER.info("Transcript unavailable for video '%s' (%s): %s", video_id, channel.name, exc)

        if not transcript:
            missing_transcripts += 1

        records.append(_to_record(video, channel=channel, transcript=transcript))

    return sorted(records, key=lambda record: (record.channel_id, record.video_id, record.title)), missing_transcripts, None




def _is_dns_resolution_error(exc: Exception) -> bool:
    """Return ``True`` when an exception chain contains a DNS lookup failure."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, gaierror):
            return True
        if isinstance(current, URLError) and isinstance(current.reason, gaierror):
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False

def fetch_all_channels(
    channel_configs: Iterable[YouTubeChannelConfig],
    *,
    api_key: str,
    provider_base_url: str | None = None,
) -> tuple[list[YouTubeRecord], int, int]:
    """Poll all configured channels and return records + failed channels + missing transcripts."""

    default_base_url = "https://transcriptapi.com/api/v2"
    configured_base_url = provider_base_url or os.getenv("TRANSCRIPT_API_BASE_URL", default_base_url)

    records: list[YouTubeRecord] = []
    failed_channels = 0
    missing_transcripts = 0

    for channel in channel_configs:
        channel_records, channel_missing, error = poll_channel_videos(
            channel,
            api_key=api_key,
            provider_base_url=configured_base_url,
        )

        # Fallback for misconfigured/invalid hostnames in TRANSCRIPT_API_BASE_URL.
        if error is not None and configured_base_url != default_base_url and _is_dns_resolution_error(error):
            LOGGER.warning(
                "Transcript provider hostname lookup failed for '%s'; retrying '%s' with default endpoint.",
                configured_base_url,
                channel.name,
            )
            channel_records, channel_missing, error = poll_channel_videos(
                channel,
                api_key=api_key,
                provider_base_url=default_base_url,
            )

        failed_channels += int(error is not None)
        missing_transcripts += channel_missing
        records.extend(channel_records)

    return sorted(records, key=lambda record: (record.channel_id, record.video_id, record.title)), failed_channels, missing_transcripts
