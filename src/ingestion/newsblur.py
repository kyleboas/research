"""NewsBlur API ingestion: session-cookie auth, river of news stories, optional full-text enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from http.cookiejar import CookieJar
import json
import logging
import os
import time
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .rss import RSSRecord

LOGGER = logging.getLogger("research.ingestion.newsblur")

NEWSBLUR_BASE_URL = "https://www.newsblur.com"

# Maximum feed IDs per river_stories request (keeps URL length reasonable)
_FEED_BATCH_SIZE = 100


@dataclass(frozen=True)
class NewsBlurConfig:
    """Configuration for NewsBlur API access."""

    username: str
    password: str
    base_url: str = NEWSBLUR_BASE_URL
    timeout_s: float = 15.0
    retries: int = 2
    backoff_base_s: float = 1.0
    latest_limit: int = 50
    fetch_original_text: bool = True


class NewsBlurClient:
    """NewsBlur API client using session-cookie authentication.

    Instantiate, call ``login()``, then use the fetch methods.
    """

    def __init__(self, config: NewsBlurConfig) -> None:
        self._config = config
        self._cookie_jar: CookieJar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookie_jar))

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> None:
        """POST to /api/login and store the session cookie."""
        url = f"{self._config.base_url}/api/login"
        body = urlencode({"username": self._config.username, "password": self._config.password}).encode("utf-8")
        request = Request(url, data=body, method="POST")
        self._request_with_retry(request, endpoint="api/login")
        LOGGER.info("NewsBlur login successful for user '%s'", self._config.username)

    # ------------------------------------------------------------------
    # Feed discovery
    # ------------------------------------------------------------------

    def fetch_feed_ids(self) -> list[int]:
        """Return the integer IDs of all subscribed feeds."""
        url = f"{self._config.base_url}/reader/feeds"
        request = Request(url)
        raw = self._request_with_retry(request, endpoint="reader/feeds")
        data = json.loads(raw)
        feeds = data.get("feeds", {})
        return sorted(int(fid) for fid in feeds.keys())

    # ------------------------------------------------------------------
    # Story retrieval
    # ------------------------------------------------------------------

    def fetch_river_stories(
        self,
        feed_ids: list[int],
        *,
        read_filter: str = "unread",
        page: int = 1,
    ) -> list[dict]:
        """Fetch stories from the river of news for the given feed IDs.

        ``read_filter`` accepts ``"unread"``, ``"read"``, or ``"all"``.
        """
        parts = [f"feeds={fid}" for fid in feed_ids]
        parts.append(f"read_filter={read_filter}")
        parts.append(f"page={page}")
        url = f"{self._config.base_url}/reader/river_stories?{'&'.join(parts)}"
        request = Request(url)
        raw = self._request_with_retry(request, endpoint="reader/river_stories")
        data = json.loads(raw)
        return data.get("stories", [])

    def fetch_original_text_for_story(self, story_hash: str) -> str:
        """Return the reader-mode extracted full text for *story_hash*.

        Returns an empty string on failure so callers can fall back gracefully.
        """
        url = f"{self._config.base_url}/rss_feeds/original_text?story_hash={story_hash}"
        request = Request(url)
        try:
            raw = self._request_with_retry(request, endpoint="rss_feeds/original_text")
            data = json.loads(raw)
            return (data.get("original_text") or "").strip()
        except Exception as exc:
            LOGGER.warning("original_text fetch failed for hash '%s': %s", story_hash, exc)
            return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request_with_retry(self, request: Request, *, endpoint: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self._config.retries + 1):
            try:
                with self._opener.open(request, timeout=self._config.timeout_s) as response:
                    return response.read()
            except Exception as exc:
                last_error = exc
                LOGGER.debug(
                    "NewsBlur '%s' attempt %d/%d failed: %s",
                    endpoint,
                    attempt + 1,
                    self._config.retries + 1,
                    exc,
                )
                if attempt < self._config.retries:
                    time.sleep(self._config.backoff_base_s * (2**attempt))
        assert last_error is not None
        raise last_error


# ---------------------------------------------------------------------------
# Record conversion
# ---------------------------------------------------------------------------


def _story_to_rss_record(story: dict, *, fetch_full_text: "((str) -> str) | None" = None) -> RSSRecord:
    """Convert a NewsBlur story dict to a normalised :class:`RSSRecord`."""

    title = (story.get("story_title") or "").strip()
    url = (story.get("story_permalink") or "").strip()
    feed_id = story.get("story_feed_id")
    feed_name = str(feed_id) if feed_id is not None else ""
    guid_raw = story.get("id") or story.get("story_hash") or None
    guid = str(guid_raw).strip() if guid_raw is not None else None

    # Published timestamp — NewsBlur provides both a human-readable string
    # ("story_date") and a Unix integer ("story_timestamp").
    published_at: datetime | None = None
    ts_raw = story.get("story_timestamp")
    date_raw = story.get("story_date")
    if isinstance(ts_raw, (int, float)) and ts_raw > 0:
        try:
            published_at = datetime.fromtimestamp(float(ts_raw), tz=UTC)
        except (OSError, OverflowError, ValueError):
            pass
    if published_at is None and date_raw:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                published_at = datetime.strptime(str(date_raw).strip(), fmt).replace(tzinfo=UTC)
                break
            except ValueError:
                continue

    # Content — prefer the full ``story_content``, fall back to summary.
    content = (story.get("story_content") or story.get("story_summary") or "").strip()

    # Optionally enrich with reader-mode full text when content is absent.
    if fetch_full_text and not content:
        story_hash = story.get("story_hash") or ""
        if story_hash:
            content = fetch_full_text(story_hash)

    # Stable source key mirrors the logic in rss._stable_source_key.
    if guid:
        source_key = f"guid:{guid}"
    elif url:
        source_key = f"url:{url}"
    else:
        digest_input = "|".join((feed_name, title, published_at.isoformat() if published_at else ""))
        source_key = f"hash:{hashlib.sha256(digest_input.encode('utf-8')).hexdigest()}"

    return RSSRecord(
        source_type="rss",
        source_key=source_key,
        title=title,
        url=url,
        published_at=published_at,
        content=content,
        feed_name=feed_name,
        guid=guid,
    )


# ---------------------------------------------------------------------------
# Top-level fetch function
# ---------------------------------------------------------------------------


def fetch_newsblur_records(config: NewsBlurConfig) -> tuple[list[RSSRecord], Exception | None]:
    """Login to NewsBlur, fetch unread river stories, and return :class:`RSSRecord` list.

    Returns ``(records, None)`` on success or ``([], exc)`` on failure so that
    callers can decide whether to treat a NewsBlur outage as fatal.
    """
    try:
        client = NewsBlurClient(config)
        client.login()

        feed_ids = client.fetch_feed_ids()
        if not feed_ids:
            LOGGER.warning("NewsBlur: no subscribed feeds found for user '%s'.", config.username)
            return [], None

        # Fetch stories in batches to keep request URLs manageable.
        all_stories: list[dict] = []
        for i in range(0, len(feed_ids), _FEED_BATCH_SIZE):
            batch = feed_ids[i : i + _FEED_BATCH_SIZE]
            stories = client.fetch_river_stories(batch, read_filter="unread")
            all_stories.extend(stories)
            if len(all_stories) >= config.latest_limit:
                break

        # Sort newest-first by Unix timestamp then cap at latest_limit.
        all_stories.sort(key=lambda s: s.get("story_timestamp") or 0, reverse=True)
        all_stories = all_stories[: config.latest_limit]

        fetcher = client.fetch_original_text_for_story if config.fetch_original_text else None

        records = sorted(
            [_story_to_rss_record(story, fetch_full_text=fetcher) for story in all_stories],
            key=lambda r: (r.source_key, r.url, r.title),
        )

        LOGGER.info(
            "NewsBlur: fetched %d stories across %d subscribed feeds.",
            len(records),
            len(feed_ids),
        )
        return records, None

    except Exception as exc:
        LOGGER.warning("NewsBlur ingestion failed: %s", exc)
        return [], exc


# ---------------------------------------------------------------------------
# Env loader
# ---------------------------------------------------------------------------


def load_newsblur_config_from_env() -> NewsBlurConfig | None:
    """Return a :class:`NewsBlurConfig` built from environment variables.

    Returns ``None`` when ``NEWSBLUR_USERNAME`` or ``NEWSBLUR_PASSWORD`` is
    absent, so callers can skip NewsBlur gracefully.
    """
    username = os.getenv("NEWSBLUR_USERNAME", "").strip()
    password = os.getenv("NEWSBLUR_PASSWORD", "").strip()
    if not username or not password:
        return None

    return NewsBlurConfig(
        username=username,
        password=password,
        base_url=os.getenv("NEWSBLUR_BASE_URL", NEWSBLUR_BASE_URL),
        timeout_s=float(os.getenv("NEWSBLUR_TIMEOUT_S", "15")),
        retries=int(os.getenv("NEWSBLUR_RETRIES", "2")),
        backoff_base_s=float(os.getenv("NEWSBLUR_BACKOFF_BASE_S", "1.0")),
        latest_limit=int(os.getenv("NEWSBLUR_LATEST_LIMIT", "50")),
        fetch_original_text=os.getenv("NEWSBLUR_FETCH_ORIGINAL_TEXT", "true").lower() not in ("0", "false", "no"),
    )
