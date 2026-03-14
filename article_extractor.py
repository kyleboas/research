"""Full-text article extraction from URLs.

RSS feeds often only expose summaries or truncated content. This module
treats RSS as discovery-only and fetches the linked page to extract the
full article body using a cascade of extractors:

  1. trafilatura (primary) — purpose-built for web article extraction
  2. readability-lxml (fallback) — Mozilla Readability port, good for news sites

Also extracts structured metadata: author, publish date, sitename, etc.
"""

import os
import logging
import re
import threading
import time
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import quote

log = logging.getLogger("research")
DEFUDDLE_BASE_URL = "https://defuddle.md/"
RETRYABLE_HTTP_STATUSES = {408, 429, 503}
DEFUDDLE_MIN_INTERVAL_SECONDS = max(0.0, float(os.environ.get("DEFUDDLE_MIN_INTERVAL_SECONDS", "2.0")))

# Lazy imports — these are optional dependencies that gracefully degrade
_trafilatura = None
_readability = None
_defuddle_lock = threading.Lock()
_defuddle_next_allowed_at = 0.0


def _get_trafilatura():
    global _trafilatura
    if _trafilatura is None:
        try:
            import trafilatura
            _trafilatura = trafilatura
        except ImportError:
            _trafilatura = False
    return _trafilatura if _trafilatura is not False else None


def _get_readability():
    global _readability
    if _readability is None:
        try:
            from readability import Document
            _readability = Document
        except ImportError:
            _readability = False
    return _readability if _readability is not False else None


def _fetch_html(url, timeout=20):
    """Fetch raw HTML from a URL with a browser-like User-Agent."""
    from urllib.request import Request, urlopen
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; ResearchBot/2.0; +football-tactics-research)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "*",
    })
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_markdown(url, timeout=20):
    from urllib.request import Request, urlopen
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ResearchBot/2.0; +football-tactics-research)",
            "Accept": "text/markdown,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "*",
        },
    )
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        _pace_defuddle()
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            status = int(e.code)
            if status in RETRYABLE_HTTP_STATUSES and attempt < max_attempts:
                delay = min(30.0, 5.0 * (2 ** (attempt - 1)))
                log.warning(
                    "defuddle article fetch retryable failure status=%s attempt=%s/%s url=%s; retrying in %.2fs",
                    status,
                    attempt,
                    max_attempts,
                    url,
                    delay,
                )
                time.sleep(delay)
                continue
            raise


def _strip_html(html):
    """Basic HTML tag stripping as last resort."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    return re.sub(r"<[^>]+>", "", html).strip()


def _parse_markdown_frontmatter(markdown):
    if not markdown.startswith("---\n"):
        return {}, markdown
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return {}, markdown
    metadata = {}
    for raw_line in markdown[4:end].splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
            cleaned = cleaned[1:-1]
        metadata[key.strip()] = cleaned
    return metadata, markdown[end + len("\n---\n") :]


def _clean_markdown_article(text):
    cleaned = str(text or "")
    cleaned = re.sub(r"```.*?```", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"\[(.*?)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.M)
    cleaned = re.sub(r"^\s*>\s?", "", cleaned, flags=re.M)
    cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.M)
    cleaned = re.sub(r"^\s*\d+\.\s+", "", cleaned, flags=re.M)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _normalize_publish_date(raw):
    value = str(raw or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
        return match.group(1) if match else None


def _defuddle_markdown_url(url):
    return f"{DEFUDDLE_BASE_URL}{quote(str(url or '').strip(), safe=':/?&=#')}"


def _pace_defuddle():
    global _defuddle_next_allowed_at
    if DEFUDDLE_MIN_INTERVAL_SECONDS <= 0:
        return
    with _defuddle_lock:
        now = time.monotonic()
        delay = max(0.0, _defuddle_next_allowed_at - now)
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        _defuddle_next_allowed_at = now + DEFUDDLE_MIN_INTERVAL_SECONDS


def _extract_defuddle_article(url):
    markdown = _fetch_markdown(_defuddle_markdown_url(url))
    metadata, body = _parse_markdown_frontmatter(markdown)
    content = _clean_markdown_article(body)
    if len(content) <= 200:
        return None
    return {
        "content": content,
        "author": str(metadata.get("author") or "").strip() or None,
        "publish_date": _normalize_publish_date(
            metadata.get("published")
            or metadata.get("publish_date")
            or metadata.get("date")
        ),
        "sitename": str(
            metadata.get("sitename")
            or metadata.get("site_name")
            or metadata.get("source")
            or ""
        ).strip() or None,
        "title": str(metadata.get("title") or "").strip() or None,
        "extraction_method": "defuddle",
    }


def extract_article(url, fallback_content=None):
    """Extract full article text and metadata from a URL.

    Args:
        url: The article URL to fetch and extract from.
        fallback_content: RSS-provided content to use if extraction fails.

    Returns:
        dict with keys:
            content: Full article text (cleaned, no HTML)
            author: Author name if found
            publish_date: ISO date string if found
            sitename: Site/publication name if found
            title: Article title extracted from the page (may differ from RSS title)
            extraction_method: Which extractor succeeded ('trafilatura', 'readability', 'fallback')
    """
    result = {
        "content": fallback_content or "",
        "author": None,
        "publish_date": None,
        "sitename": None,
        "title": None,
        "extraction_method": "fallback",
    }

    if not url:
        return result

    try:
        defuddled = _extract_defuddle_article(url)
        if defuddled:
            result.update(defuddled)
            log.debug("defuddle extracted %d chars from %s", len(result["content"]), url)
            return result
    except Exception as e:
        log.debug("defuddle failed for %s: %s", url, e)

    # Fetch the HTML
    try:
        html = _fetch_html(url)
    except Exception as e:
        log.debug("Could not fetch %s for full-text extraction: %s", url, e)
        return result

    # Try trafilatura first — best extraction quality
    trafilatura = _get_trafilatura()
    if trafilatura:
        try:
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                favor_precision=False,
                favor_recall=True,
                url=url,
            )
            if extracted and len(extracted) > 200:
                result["content"] = extracted
                result["extraction_method"] = "trafilatura"

                # Extract metadata via trafilatura
                metadata = trafilatura.extract_metadata(html, default_url=url)
                if metadata:
                    result["author"] = metadata.author or None
                    result["sitename"] = metadata.sitename or None
                    result["title"] = metadata.title or None
                    if metadata.date:
                        result["publish_date"] = str(metadata.date)

                log.debug("trafilatura extracted %d chars from %s", len(extracted), url)
                return result
        except Exception as e:
            log.debug("trafilatura failed for %s: %s", url, e)

    # Try readability-lxml as fallback
    ReadabilityDoc = _get_readability()
    if ReadabilityDoc:
        try:
            if isinstance(html, bytes):
                html_str = html.decode("utf-8", errors="replace")
            else:
                html_str = html
            doc = ReadabilityDoc(html_str, url=url)
            summary_html = doc.summary()
            clean_text = _strip_html(summary_html)
            if clean_text and len(clean_text) > 200:
                result["content"] = clean_text
                result["title"] = doc.short_title() or None
                result["extraction_method"] = "readability"
                log.debug("readability extracted %d chars from %s", len(clean_text), url)
                return result
        except Exception as e:
            log.debug("readability failed for %s: %s", url, e)

    # If all extractors failed but we got HTML, do basic stripping
    if html:
        stripped = _strip_html(html)
        if stripped and len(stripped) > len(result["content"]):
            result["content"] = stripped
            result["extraction_method"] = "html_strip"

    return result


def should_extract(url, rss_content):
    """Decide whether full-text extraction is likely to improve on RSS content.

    Heuristic: if the RSS content is short (< 500 words), the feed probably
    only provided a summary, so fetching the full article is worthwhile.
    """
    if not url:
        return False
    word_count = len((rss_content or "").split())
    return word_count < 500
