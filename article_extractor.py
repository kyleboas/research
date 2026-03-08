"""Full-text article extraction from URLs.

RSS feeds often only expose summaries or truncated content. This module
treats RSS as discovery-only and fetches the linked page to extract the
full article body using a cascade of extractors:

  1. trafilatura (primary) — purpose-built for web article extraction
  2. readability-lxml (fallback) — Mozilla Readability port, good for news sites

Also extracts structured metadata: author, publish date, sitename, etc.
"""

import logging
import re
from datetime import datetime

log = logging.getLogger("research")

# Lazy imports — these are optional dependencies that gracefully degrade
_trafilatura = None
_readability = None


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


def _strip_html(html):
    """Basic HTML tag stripping as last resort."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    return re.sub(r"<[^>]+>", "", html).strip()


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
