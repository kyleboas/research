#!/usr/bin/env python3
"""Football research pipeline: ingest → detect trends → multi-agent deep research report.

Architecture mirrors Anthropic's production research system:
  LeadResearcher (extended thinking) → parallel Subagents (OODA retrieval)
  → Synthesis → Sufficiency evaluation → optional re-plan → CitationAgent → Revision
"""

import argparse, json, logging, math, os, platform, random, re, socket, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
import xml.etree.ElementTree as ET

import openai, psycopg
from dotenv import load_dotenv
from db_conn import resolve_database_conninfo
from trend_detection import run_bertrend_detection, describe_signals_with_llm
from article_extractor import extract_article, should_extract
from tactical_extraction import chunk_with_context, extract_tactical_patterns, extract_tactical_context
from novelty_scoring import compute_novelty_score, update_baseline, score_tactical_pattern_novelty

log = logging.getLogger("research")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TRANSCRIPT_KEY = os.environ.get("TRANSCRIPT_API_KEY", "")
NEWSBLUR_USERNAME = os.environ.get("NEWSBLUR_USERNAME", "")
NEWSBLUR_PASSWORD = os.environ.get("NEWSBLUR_PASSWORD", "")

# ── Cloudflare AI Gateway ──────────────────────────────────────────────────────
CLOUDFLARE_GATEWAY_URL = os.environ.get("CLOUDFLARE_GATEWAY_URL", "")
CLOUDFLARE_GATEWAY_TOKEN = os.environ.get("CLOUDFLARE_GATEWAY_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ── Config file (config.json) overrides env-var model defaults ────────────────
_cfg_path = ROOT / "config.json"
_CFG: dict = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}

LEAD_MODEL    = os.environ.get("LEAD_MODEL")    or _CFG.get("lead_model",    "deepseek/deepseek-r1")
MODEL         = os.environ.get("MODEL")         or _CFG.get("model",         "workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast")
EMBED_MODEL   = os.environ.get("EMBED_MODEL")   or _CFG.get("embed_model",   "openai/text-embedding-3-small")
SIGNAL_MODEL  = os.environ.get("SIGNAL_MODEL")  or _CFG.get("signal_model",  "workers-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast")

# ── Per-step model overrides (budget-conscious defaults use Sonnet) ───────────
SYNTHESIS_MODEL = os.environ.get("SYNTHESIS_MODEL") or _CFG.get("synthesis_model", "anthropic/claude-sonnet-4-6")
SUMMARY_MODEL   = os.environ.get("SUMMARY_MODEL")  or _CFG.get("summary_model",   "anthropic/claude-sonnet-4-6")
REVISION_MODEL  = os.environ.get("REVISION_MODEL")  or _CFG.get("revision_model",  "anthropic/claude-sonnet-4-6")
CITATION_MODEL  = os.environ.get("CITATION_MODEL")  or _CFG.get("citation_model",  "google/gemini-2.5-flash")
EVAL_MODEL      = os.environ.get("EVAL_MODEL")      or _CFG.get("eval_model",      "google/gemini-2.5-flash")


def _validate_required_env(step: str):
    common_required = ["CLOUDFLARE_GATEWAY_URL", "CLOUDFLARE_GATEWAY_TOKEN"]
    step_required = {
        "ingest": common_required + ["NEWSBLUR_USERNAME", "NEWSBLUR_PASSWORD", "TRANSCRIPT_API_KEY"],
        "backfill": common_required,
        "detect": common_required,
        "report": common_required,
        "all": common_required + ["NEWSBLUR_USERNAME", "NEWSBLUR_PASSWORD", "TRANSCRIPT_API_KEY"],
    }
    missing = [name for name in step_required.get(step, []) if not os.environ.get(name)]
    if missing:
        log.error("Missing required environment variables for step '%s': %s", step, ", ".join(missing))
        log.error("Set the missing variables in your runtime environment and retry.")
        raise SystemExit(2)


def _normalize_cloudflare_base_urls(raw_url: str):
    """Return SDK-safe chat/embeddings base URLs from a Cloudflare Gateway URL.

    Supported inputs:
    - .../openai             → kept as-is (OpenAI-only route)
    - .../openai/chat/completions → trimmed to .../openai
    - .../compat/chat/completions → trimmed to .../compat
    - .../compat             → kept as-is (unified route, supports all providers via model prefix)

    The /compat endpoint is the unified AI Gateway route. Model names must be
    prefixed with the provider (e.g. "openai/gpt-4o", "workers-ai/@cf/meta/llama-...").
    """
    base = raw_url.rstrip("/")

    if base.endswith("/openai/chat/completions"):
        provider = base[: -len("/chat/completions")]
        return provider, provider

    if base.endswith("/openai"):
        return base, base

    if base.endswith("/compat/chat/completions"):
        return base[: -len("/chat/completions")], base[: -len("/chat/completions")]

    if base.endswith("/compat"):
        return base, base

    return base, base


def _make_client(base_url: str):
    """Build an OpenAI-compatible client routed through Cloudflare AI Gateway."""
    return openai.OpenAI(api_key="", base_url=base_url)


_PROVIDER_API_KEYS = {
    "anthropic": ANTHROPIC_API_KEY,
    "deepseek": DEEPSEEK_API_KEY,
    "openai": OPENAI_API_KEY,
}

def _model_provider(model_name: str) -> str:
    model = (model_name or "").strip()
    if "/" not in model:
        return ""
    provider, _ = model.split("/", 1)
    return provider


def _provider_api_key_for_model(model_name: str) -> str:
    return _PROVIDER_API_KEYS.get(_model_provider(model_name), "").strip()


def _provider_api_env_for_model(model_name: str) -> str:
    provider = _model_provider(model_name)
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    if provider == "deepseek":
        return "DEEPSEEK_API_KEY"
    if provider == "openai":
        return "OPENAI_API_KEY"
    return ""


def _log_auth_hint_for_model(model_name: str):
    env_name = _provider_api_env_for_model(model_name)
    provider = _model_provider(model_name)
    if env_name and not os.environ.get(env_name):
        log.error(
            "Model '%s' failed authentication. Add %s or configure %s access in Cloudflare AI Gateway.",
            model_name,
            env_name,
            provider or "provider",
        )


def _resolve_embed_model(base_url: str, model_name: str) -> str:
    """Normalize embed model names for Cloudflare AI Gateway routes.

    Compat route expects provider-prefixed model names, e.g.:
      - workers-ai/@cf/baai/bge-m3
      - openai/text-embedding-3-small
    """
    base = (base_url or "").rstrip("/")
    model = (model_name or "").strip()
    if not model:
        return model

    if "/compat" in base:
        if model.startswith("@cf/"):
            return f"workers-ai/{model}"
        if "/" not in model:
            if model.startswith("text-embedding-"):
                return f"openai/{model}"
            return f"workers-ai/{model}"
    return model


def _is_bad_format_error(exc: Exception) -> bool:
    """Detect Cloudflare compat payload-format errors (code 2019)."""
    msg = str(exc).lower()
    return "bad format" in msg or "'code': 2019" in msg or '"code": 2019' in msg


def _chat_completion_create(*, model: str, max_tokens: int, messages: list[dict], reasoning_effort: str | None = None):
    """Create a chat completion against the exact configured model path."""
    model_name = (model or "").strip()
    client = get_chat_client(model_name)
    # max_completion_tokens is only valid for OpenAI reasoning models (o-series).
    # All other providers (Gemini, Claude, Workers AI, etc.) use the standard
    # max_tokens parameter; sending max_completion_tokens causes a 400 "Chat
    # completion bad format" error (Cloudflare error code 2019).
    tokens_key = "max_completion_tokens" if reasoning_effort else "max_tokens"
    kwargs = {
        "model": model_name,
        tokens_key: max_tokens,
        "messages": messages,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    def _call_with_bad_format_retries(request_kwargs: dict):
        """Retry with alternate token key/payload shape for strict compat providers."""
        try:
            return client.chat.completions.create(**request_kwargs)
        except Exception as bad_format_exc:
            if not _is_bad_format_error(bad_format_exc):
                raise

        # Some compat providers reject one token field but accept the other.
        # Try the alternate key next.
        alt = dict(request_kwargs)
        if "max_tokens" in alt:
            alt["max_completion_tokens"] = alt.pop("max_tokens")
        elif "max_completion_tokens" in alt:
            alt["max_tokens"] = alt.pop("max_completion_tokens")
        try:
            return client.chat.completions.create(**alt)
        except Exception as bad_format_exc:
            if not _is_bad_format_error(bad_format_exc):
                raise

        # Final fallback: drop token limit entirely (gateway/provider defaults).
        minimal = dict(alt)
        minimal.pop("max_tokens", None)
        minimal.pop("max_completion_tokens", None)
        return client.chat.completions.create(**minimal)

    try:
        return _call_with_bad_format_retries(kwargs)
    except openai.AuthenticationError:
        _log_auth_hint_for_model(model_name)
        raise


_chat_clients: dict[tuple[str, str, bool], openai.OpenAI] = {}
_embed_clients: dict[tuple[str, str, bool], openai.OpenAI] = {}
_chat_base_url, _embed_base_url = _normalize_cloudflare_base_urls(CLOUDFLARE_GATEWAY_URL)
_resolved_embed_model = _resolve_embed_model(_embed_base_url, EMBED_MODEL)


log.info("Cloudflare chat base URL: %s", _chat_base_url)
if _embed_base_url != _chat_base_url:
    log.info("Cloudflare embeddings base URL: %s", _embed_base_url)
if _resolved_embed_model != EMBED_MODEL:
    log.info("Normalized embeddings model for route compatibility: %s -> %s", EMBED_MODEL, _resolved_embed_model)
log.info("Model config: lead=%s eval=%s summary=%s synthesis=%s citation=%s revision=%s",
         LEAD_MODEL, EVAL_MODEL, SUMMARY_MODEL, SYNTHESIS_MODEL, CITATION_MODEL, REVISION_MODEL)


def _client_cache_key(base_url: str, model_name: str) -> tuple[str, str, bool]:
    provider_key = _provider_api_key_for_model(model_name)
    return (base_url, provider_key, bool(CLOUDFLARE_GATEWAY_TOKEN))


def _client_headers(model_name: str) -> dict:
    provider_key = _provider_api_key_for_model(model_name)
    if provider_key:
        return {"cf-aig-authorization": f"Bearer {CLOUDFLARE_GATEWAY_TOKEN}"} if CLOUDFLARE_GATEWAY_TOKEN else {}
    if CLOUDFLARE_GATEWAY_TOKEN:
        return {"cf-aig-authorization": f"Bearer {CLOUDFLARE_GATEWAY_TOKEN}"}
    return {}


def get_chat_client(model_name: str):
    key = _client_cache_key(_chat_base_url, model_name)
    if key not in _chat_clients:
        provider_key = _provider_api_key_for_model(model_name)
        _chat_clients[key] = openai.OpenAI(
            api_key=provider_key,
            base_url=_chat_base_url,
            default_headers=_client_headers(model_name),
        )
    return _chat_clients[key]


def get_embed_client(model_name: str = _resolved_embed_model):
    key = _client_cache_key(_embed_base_url, model_name)
    if key not in _embed_clients:
        provider_key = _provider_api_key_for_model(model_name)
        _embed_clients[key] = openai.OpenAI(
            api_key=provider_key,
            base_url=_embed_base_url,
            default_headers=_client_headers(model_name),
        )
    return _embed_clients[key]


CITATION_FMT = "Cite every claim as [S<source_id>:C<chunk_id>]. Never cite IDs not in the provided context."

# ── Static system prompts (extracted for prompt caching) ──────────────────────
# Anthropic models cache repeated prompt prefixes automatically.
# By keeping system prompts as constants, identical prefixes across calls
# hit the cache ($0.50/MTok instead of $5.00/MTok for Opus 4.6).

SYS_OODA_EVAL = (
    "You are evaluating retrieval sufficiency for a research subagent following "
    "the OODA loop (Observe-Orient-Decide-Act).\n\n"
    "OBSERVE: Review the chunks collected so far.\n"
    "ORIENT: Compare against the research objective — what's covered vs what's missing?\n"
    "DECIDE: Is evidence sufficient, or do we need another retrieval round?\n\n"
    "If more retrieval is needed, generate a query that is NARROWER and MORE SPECIFIC "
    "than previous queries — do not repeat broad searches."
)

SYS_SUBAGENT = (
    f"You are a focused research subagent. {CITATION_FMT}\n\n"
    "Stay strictly within your assigned boundaries. Do not speculate beyond "
    "what the evidence supports. If evidence is thin, say so explicitly."
)

SYS_SYNTHESIS = (
    f"You are a synthesis editor merging multiple subagent research outputs into "
    f"one coherent, publication-quality research report. {CITATION_FMT}\n\n"
    "Always write the report in English, regardless of the language of the source material."
)

SYS_CITATION = (
    "You are a CitationAgent. Your SOLE job is to verify citations in a research report.\n\n"
    "For EVERY citation [S<source_id>:C<chunk_id>] in the report:\n"
    "1. Verify the source_id and chunk_id exist in the provided chunks\n"
    "2. Verify the cited claim is actually supported by that chunk's content\n"
    "3. Check for claims that SHOULD have citations but don't\n"
    "4. Check for fabricated/hallucinated citation IDs\n\n"
    "You must also verify the Sources section at the end lists accurate titles and URLs."
)

SYS_REVISION = (
    f"You are a revision editor producing the final research report. {CITATION_FMT}\n\n"
    "You have received a citation verification report from the CitationAgent. "
    "Apply every directive precisely. The final report must have zero citation errors.\n\n"
    "Always write the report in English, regardless of the language of the source material."
)

SYS_DECOMPOSE = (
    "You are a LeadResearcher orchestrating a multi-agent deep research system. "
    "Your job is to decompose the research topic into non-overlapping subagent tasks "
    "with clear boundaries. Each subagent will run independently with its own context window.\n\n"
    "EFFORT SCALING RULES:\n"
    "- Simple fact-finding: 1-2 subagents, max_rounds=2, 3-5 search queries each\n"
    "- Moderate analysis: 3-4 subagents, max_rounds=3, 3-5 search queries each\n"
    "- Complex multi-faceted research: 5-7 subagents, max_rounds=5, 4-6 search queries each\n\n"
    "You MUST assess which complexity level applies and set parameters accordingly."
)

SYS_SUFFICIENCY = (
    "You are the LeadResearcher evaluating whether the synthesized research is sufficient "
    "or whether additional subagent research rounds are needed.\n\n"
    "Be critical but pragmatic. Only request additional research if there are SPECIFIC, "
    "ACTIONABLE gaps that more retrieval could realistically fill."
)

# ══════════════════════════════════════════════
# Feed parsing (YouTube only)
# ══════════════════════════════════════════════

def parse_youtube(path):
    text = path.read_text()
    pairs = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        match = re.match(r"^(.+?):\s*(https?://\S+)$", line)
        if match:
            name = match.group(1).strip()
            channel_source = match.group(2).strip()
            channel_id = _extract_uc_channel_id(channel_source)
            if channel_id:
                pairs.append((name, channel_id))
            else:
                log.warning("YouTube config line has no UC channel id: %s", raw_line)

    if pairs:
        return pairs

    # Backward-compatible fallback for older markdown list format.
    names = [m.group(1) for m in re.finditer(r"^-\s+\*\*(.+?)\*\*", text, re.M)]
    cids = [m.group(1) for m in re.finditer(r"^\s+-\s+(?:Canonical\s+)?Channel ID:\s*(\S+)", text, re.M)]
    return list(zip(names, cids))


def strip_html(html):
    return re.sub(r"<[^>]+>", "", html).strip()


TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref", "source")


def canonicalize_url(url: str) -> str:
    """Return a canonical URL used for ingest diagnostics.

    This does not affect dedupe behavior (which is currently source_key based);
    it exists to make duplicate/new-source logs easier to reason about.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    clean_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(k.lower().startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES)
    ]
    normalized_path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), normalized_path, urlencode(clean_query), ""))

# ══════════════════════════════════════════════
# NewsBlur RSS ingestion
# ══════════════════════════════════════════════

def _get(url, headers=None, timeout=15):
    req = Request(url, headers=headers or {"User-Agent": "ResearchBot/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()

def _newsblur_session():
    """Login to NewsBlur and return a cookie-enabled URL opener."""
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    data = urlencode({"username": NEWSBLUR_USERNAME, "password": NEWSBLUR_PASSWORD}).encode()
    req = Request("https://newsblur.com/api/login", data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "ResearchBot/1.0")
    with opener.open(req, timeout=15) as r:
        result = json.loads(r.read())
    if not result.get("authenticated"):
        raise RuntimeError(f"NewsBlur login failed: {result.get('errors', result)}")
    return opener

def fetch_newsblur(since_ts=None):
    """Fetch recent stories from NewsBlur river of news.

    Treats RSS as discovery: fetches the feed for URLs and metadata, then
    uses full-text extraction to get the actual article body when the RSS
    content looks truncated (< 500 words).

    Args:
        since_ts: Optional Unix timestamp (int/float). When provided, only
                  stories newer than this time are fetched via NewsBlur's
                  ``newer_than`` parameter so repeat runs don't re-process
                  stories that were already ingested.
    """
    try:
        opener = _newsblur_session()
    except Exception as e:
        log.warning("NewsBlur login failed: %s", e)
        return []
    try:
        url = "https://newsblur.com/reader/river_stories?read_filter=all&order=newest&limit=100"
        if since_ts is not None:
            url += f"&newer_than={int(since_ts)}"
        req = Request(url)
        req.add_header("User-Agent", "ResearchBot/1.0")
        with opener.open(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        log.warning("NewsBlur river fetch failed: %s", e)
        return []
    items = []
    for story in data.get("stories", []):
        title = (story.get("story_title") or "").strip()
        url = (story.get("story_permalink") or "").strip()
        rss_content = strip_html(story.get("story_content") or story.get("story_summary") or "")
        story_id = str(story.get("id") or url).strip()
        feed_id = str(story.get("story_feed_id") or "").strip()
        if not rss_content:
            continue

        # Full-text extraction: if RSS content is short, fetch the real article
        content = rss_content
        author = None
        publish_date = None
        sitename = None
        extraction_method = "rss"

        if should_extract(url, rss_content):
            try:
                article = extract_article(url, fallback_content=rss_content)
                if len(article["content"]) > len(rss_content):
                    content = article["content"]
                    extraction_method = article["extraction_method"]
                    log.info("Full-text extraction improved %s: %d→%d chars (%s)",
                             title[:40], len(rss_content), len(content), extraction_method)
                author = article.get("author")
                publish_date = article.get("publish_date")
                sitename = article.get("sitename")
                if article.get("title") and not title:
                    title = article["title"]
            except Exception as e:
                log.debug("Full-text extraction failed for %s: %s", url, e)

        items.append({
            "title": title,
            "url": url,
            "content": content,
            "key": f"nb:{feed_id}:{story_id}",
            "author": author,
            "publish_date": publish_date,
            "sitename": sitename,
            "extraction_method": extraction_method,
        })
    return items

# ══════════════════════════════════════════════
# YouTube ingestion
# ══════════════════════════════════════════════

TRANSCRIPT_API_BASE_URL = "https://transcriptapi.com/api/v2"
RETRYABLE_HTTP_STATUSES = {408, 429, 503}
NON_RETRYABLE_HTTP_STATUSES = {400, 401, 402, 403, 404, 422}
YOUTUBE_RSS_BASE_URL = "https://www.youtube.com/feeds/videos.xml"
TRANSCRIPT_API_USER_AGENT = "ResearchBot/1.0"
YOUTUBE_RSS_USER_AGENT = "ResearchBot/1.0"

_OUTBOUND_IP_CACHE = None


class TranscriptApiHardDeny(Exception):
    """TranscriptAPI returned a hard deny that should never be retried."""


def _host_container_metadata():
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "container_id": os.environ.get("HOSTNAME", ""),
    }


def _get_outbound_ip():
    global _OUTBOUND_IP_CACHE
    if _OUTBOUND_IP_CACHE is not None:
        return _OUTBOUND_IP_CACHE

    try:
        req = Request("https://api64.ipify.org?format=json", headers={"User-Agent": TRANSCRIPT_API_USER_AGENT})
        with urlopen(req, timeout=3) as response:
            payload = json.loads(response.read())
        _OUTBOUND_IP_CACHE = str(payload.get("ip") or "unknown")
    except Exception:
        _OUTBOUND_IP_CACHE = "unknown"
    return _OUTBOUND_IP_CACHE


def _is_hard_deny(status, body):
    if status != 403:
        return False
    normalized = (body or "").lower()
    return (
        "error_code=1010" in normalized
        or '"error_code":1010' in normalized
        or '"error_code": 1010' in normalized
        or "retryable=false" in normalized
        or '"retryable":false' in normalized
        or '"retryable": false' in normalized
    )


def _write_support_packet(payload):
    out_dir = ROOT / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    packet_file = out_dir / "transcriptapi_support.jsonl"
    with packet_file.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")


def _http_error_details(err):
    body = ""
    headers = {}
    try:
        body = err.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    try:
        headers = dict(err.headers.items()) if err.headers else {}
    except Exception:
        headers = {}
    return body, headers


def _transcriptapi_get(path, params):
    url = f"{TRANSCRIPT_API_BASE_URL}{path}?{urlencode(params)}"
    request_headers = {
        "Authorization": f"Bearer {TRANSCRIPT_KEY}",
        "Accept": "application/json",
        "User-Agent": TRANSCRIPT_API_USER_AGENT,
    }
    req = Request(
        url,
        headers=request_headers,
    )

    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(req, timeout=30) as response:
                return json.loads(response.read())
        except HTTPError as e:
            status = int(e.code)
            body, headers = _http_error_details(e)
            timestamp = datetime.now(UTC).isoformat()
            if _is_hard_deny(status, body):
                support_payload = {
                    "classification": "HARD_DENY/NON_RETRYABLE",
                    "ray_id": headers.get("CF-RAY") or headers.get("cf-ray") or "",
                    "timestamp": timestamp,
                    "endpoint": path,
                    "request_params": params,
                    "response_body": body[:3000],
                    "user_agent": request_headers.get("User-Agent"),
                    "host_container_metadata": _host_container_metadata(),
                }
                _write_support_packet(support_payload)
                log.error(
                    "TranscriptAPI hard deny classification=HARD_DENY/NON_RETRYABLE ray_id=%s timestamp=%s endpoint=%s channel_id=%s outbound_ip=%s user_agent=%s",
                    support_payload["ray_id"],
                    timestamp,
                    path,
                    params.get("channel_id") or params.get("channel") or "",
                    _get_outbound_ip(),
                    support_payload["user_agent"],
                )
                raise TranscriptApiHardDeny("TranscriptAPI hard deny")

            if status in RETRYABLE_HTTP_STATUSES and attempt < max_attempts:
                delay = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.2)
                log.warning(
                    "TranscriptAPI retryable failure path=%s status=%s attempt=%s/%s; retrying in %.2fs",
                    path,
                    status,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue

            # Log full details for non-retryable and undocumented errors.
            level = log.warning if status in NON_RETRYABLE_HTTP_STATUSES else log.error
            level(
                "TranscriptAPI request failed path=%s status=%s params=%s headers=%s body=%s",
                path,
                status,
                params,
                headers,
                body[:3000],
            )
            raise


def _youtube_rss_latest_videos(channel_id, limit=10):
    feed_url = f"{YOUTUBE_RSS_BASE_URL}?{urlencode({'channel_id': channel_id})}"
    req = Request(feed_url, headers={"User-Agent": YOUTUBE_RSS_USER_AGENT})
    with urlopen(req, timeout=30) as response:
        xml_body = response.read()

    root = ET.fromstring(xml_body)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    videos = []
    for entry in root.findall("atom:entry", ns):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        if not video_id:
            continue
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        link = ""
        link_node = entry.find("atom:link", ns)
        if link_node is not None:
            link = str(link_node.attrib.get("href") or "").strip()
        videos.append({"id": video_id, "title": title, "url": link})
        if len(videos) >= limit:
            break
    return videos


def _extract_transcript_text(data):
    for key in ("transcript", "text", "content"):
        value = data.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(p.get("text", "") for p in value if isinstance(p, dict))
    return ""


def _extract_uc_channel_id(raw):
    value = str(raw or "").strip()
    if re.match(r"^UC[\w-]{20,}$", value):
        return value
    match = re.search(r"/channel/(UC[\w-]{20,})", value)
    if match:
        return match.group(1)
    return ""


def _resolve_uc_channel_id(source):
    candidate = _extract_uc_channel_id(source)
    if candidate:
        return candidate

    cleaned = str(source or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("@"):
        cleaned = f"https://www.youtube.com/{cleaned}"
    elif not cleaned.startswith("http"):
        cleaned = f"https://www.youtube.com/{cleaned.lstrip('/')}"

    req = Request(cleaned, headers={"User-Agent": YOUTUBE_RSS_USER_AGENT})
    with urlopen(req, timeout=20) as response:
        html = response.read().decode("utf-8", errors="replace")

    found = _extract_uc_channel_id(html)
    if found:
        return found
    raise ValueError(f"Unable to resolve canonical UC channel ID from source: {source}")


def normalize_youtube_source_config(path):
    text = path.read_text()
    normalized_lines = []
    changed = False

    # Preferred simple format: "Name: https://www.youtube.com/channel/UC..."
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        match = re.match(r"^(.+?):\s*(https?://\S+)$", line)
        if not match:
            continue
        name = match.group(1).strip()
        source = match.group(2).strip()
        try:
            canonical_id = _resolve_uc_channel_id(source)
        except Exception as e:
            log.warning("YouTube source normalization failed for source=%s: %s", source, e)
            continue
        canonical_line = f"{name}: https://www.youtube.com/channel/{canonical_id}"
        normalized_lines.append(canonical_line)
        if canonical_line != line:
            changed = True

    if not normalized_lines:
        # Backward compatibility: parse old list format and rewrite.
        names = [m.group(1) for m in re.finditer(r"^-\s+\*\*(.+?)\*\*", text, re.M)]
        sources = [m.group(1) for m in re.finditer(r"^\s+-\s+(?:Canonical\s+)?Channel ID:\s*(\S+)", text, re.M)]
        for name, source in zip(names, sources):
            try:
                canonical_id = _resolve_uc_channel_id(source)
            except Exception as e:
                log.warning("YouTube source normalization failed for source=%s: %s", source, e)
                continue
            normalized_lines.append(f"{name}: https://www.youtube.com/channel/{canonical_id}")
        changed = bool(normalized_lines)

    if changed and normalized_lines:
        path.write_text("\n\n".join(normalized_lines) + "\n")
        log.info("Normalized YouTube source config to canonical UC channel URLs: %s", path)


def _iter_channel_latest_videos(payload):
    videos = payload.get("results")
    if not isinstance(videos, list):
        return []
    return videos


def _video_id(video):
    if not isinstance(video, dict):
        return ""
    for key in ("video_id", "videoId", "id"):
        value = str(video.get(key) or "").strip()
        if value:
            return value
    return ""


def _video_title(video):
    if not isinstance(video, dict):
        return ""
    for key in ("title", "name"):
        value = str(video.get(key) or "").strip()
        if value:
            return value
    return ""


def fetch_youtube(name, channel_id):
    counters = {
        "youtube_discovery_successes": 0,
        "youtube_discovery_hard_denies": 0,
        "youtube_discovery_retryable_failures": 0,
        "youtube_transcript_successes": 0,
        "youtube_transcript_failures": 0,
    }
    resolved_channel_id = _extract_uc_channel_id(channel_id)
    if not resolved_channel_id:
        log.warning("YouTube source %s has non-canonical channel id=%s", name, channel_id)
        counters["youtube_discovery_retryable_failures"] += 1
        return [], True, counters
    try:
        videos = _youtube_rss_latest_videos(resolved_channel_id)
        counters["youtube_discovery_successes"] += 1
    except TranscriptApiHardDeny:
        counters["youtube_discovery_hard_denies"] += 1
        log.warning("YouTube discovery hard-denied for %s (%s)", name, resolved_channel_id)
        return [], True, counters
    except HTTPError as e:
        if int(e.code) in RETRYABLE_HTTP_STATUSES:
            counters["youtube_discovery_retryable_failures"] += 1
        log.warning(
            "YouTube RSS discovery failed for %s (%s) status=%s",
            name,
            resolved_channel_id,
            e.code,
        )
        return [], True, counters
    except Exception as e:
        counters["youtube_discovery_retryable_failures"] += 1
        log.warning(
            "YouTube discovery failed for %s (%s): %s",
            name,
            resolved_channel_id,
            e,
        )
        return [], True, counters

    if not videos:
        log.info(
            "YouTube discovery returned no videos for %s (%s).",
            name,
            resolved_channel_id,
        )

    items = []
    for video in videos:
        vid = _video_id(video)
        if not vid:
            continue
        title = _video_title(video)
        try:
            transcript_data = _transcriptapi_get(
                "/youtube/transcript",
                {
                    "video_url": vid,
                    "format": "text",
                    "include_timestamp": "false",
                    "send_metadata": "true",
                },
            )
            transcript = _extract_transcript_text(transcript_data)
        except HTTPError as e:
            log.warning("Transcript %s failed for channel=%s title=%r status=%s", vid, name, title, e.code)
            counters["youtube_transcript_failures"] += 1
            continue
        except TranscriptApiHardDeny:
            counters["youtube_transcript_failures"] += 1
            log.warning("Transcript %s hard-denied for channel=%s title=%r", vid, name, title)
            continue
        except Exception as e:
            log.warning("Transcript %s failed for channel=%s title=%r: %s", vid, name, title, e)
            counters["youtube_transcript_failures"] += 1
            continue
        if transcript.strip():
            counters["youtube_transcript_successes"] += 1
            items.append(
                {
                    "title": title or str(transcript_data.get("title") or "").strip(),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "content": transcript.strip(),
                    "key": f"yt:{resolved_channel_id}:{vid}",
                }
            )
    return items, False, counters

# ══════════════════════════════════════════════
# Storage & embedding
# ══════════════════════════════════════════════

def source_exists_by_key(conn, source_key):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM sources WHERE source_key = %s", (source_key,))
        return cur.fetchone() is not None

def store_source(conn, item, source_type):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (source_type, source_key, title, url, content, "
            "author, publish_date, sitename, extraction_method) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (source_key) DO NOTHING RETURNING id",
            (source_type, item["key"], item["title"], item["url"], item["content"],
             item.get("author"), item.get("publish_date"), item.get("sitename"),
             item.get("extraction_method", "rss")),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None


def set_source_embed_status(conn, source_id, status, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sources "
            "SET metadata = jsonb_set("
            "  jsonb_set(COALESCE(metadata, '{}'::jsonb), '{embed_status}', to_jsonb(%s::text), true), "
            "  '{embed_updated_at}', to_jsonb(NOW()::text), true"
            ") "
            "WHERE id = %s",
            (status, source_id),
        )
        if error_message:
            cur.execute(
                "UPDATE sources "
                "SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{embed_error}', to_jsonb(%s::text), true) "
                "WHERE id = %s",
                (error_message[:500], source_id),
            )
        conn.commit()

def _sanitize_embedding_inputs(texts):
    """Normalize embedding inputs to OpenAI schema-safe strings.

    Returns a tuple of:
      - cleaned_inputs: non-empty strings that can be sent to the API
      - index_map: original positions for each cleaned input
      - total_inputs: total number of original items
    """
    if isinstance(texts, str):
        raw_items = [texts]
    else:
        raw_items = list(texts or [])

    cleaned_inputs = []
    index_map = []
    for idx, item in enumerate(raw_items):
        value = "" if item is None else str(item)
        value = value.strip()
        if not value:
            continue
        cleaned_inputs.append(value)
        index_map.append(idx)

    return cleaned_inputs, index_map, len(raw_items)


def embed(texts):
    cleaned_inputs, index_map, total_inputs = _sanitize_embedding_inputs(texts)
    if total_inputs == 0:
        log.warning("Embedding skipped: no inputs provided")
        return []
    if not cleaned_inputs:
        log.error("Embedding skipped: all inputs were empty after normalization")
        return [None] * total_inputs

    client = get_embed_client()
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.embeddings.create(model=_resolved_embed_model, input=cleaned_inputs)
            dense = [None] * total_inputs
            for source_idx, embedding_obj in zip(index_map, resp.data):
                dense[source_idx] = embedding_obj.embedding
            return dense
        except openai.BadRequestError as e:
            log.error("Embeddings request rejected (bad request — check model/config): %s", e)
            return None
        except openai.AuthenticationError as e:
            log.error(
                "Embeddings authentication failed at AI Gateway. "
                "Check CLOUDFLARE_GATEWAY_TOKEN/cf-aig-authorization and gateway credential mode: %s",
                e,
            )
            return None
        except (openai.InternalServerError, openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError) as e:
            if attempt == max_attempts:
                log.error("Embeddings request failed after %s attempts: %s", max_attempts, e)
                return None
            delay = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.2)
            log.warning(
                "Embeddings request failed (attempt %s/%s). Retrying in %.2fs: %s",
                attempt,
                max_attempts,
                delay,
                e,
            )
            time.sleep(delay)

def vec_literal(vec):
    return "[" + ",".join(str(v) for v in vec) + "]"

def chunk_and_embed(conn, source_id, text):
    """Football-aware chunking, embedding, and tactical pattern extraction.

    Uses sentence-boundary-aware chunking that preserves tactical context,
    then extracts structured tactical patterns (actor → action → zone/phase)
    from each chunk for the detection layer.
    """
    chunk_records = chunk_with_context(text)
    if not chunk_records:
        set_source_embed_status(conn, source_id, "embed_skipped", "No chunks produced from source content")
        return

    chunk_texts = [c["content"] for c in chunk_records]
    vectors = embed(chunk_texts)
    if not vectors:
        log.warning("Skipping chunk insert for source_id=%s because embeddings were unavailable", source_id)
        set_source_embed_status(conn, source_id, "embed_failed", "Embeddings unavailable (request failed or rejected)")
        return

    all_patterns = []
    try:
        with conn.cursor() as cur:
            for chunk_rec, vec in zip(chunk_records, vectors):
                if vec is None:
                    log.warning(
                        "Skipping empty chunk for source_id=%s chunk_index=%s before embedding",
                        source_id,
                        chunk_rec["chunk_index"],
                    )
                    continue
                idx = chunk_rec["chunk_index"]
                content = chunk_rec["content"]
                ctx = chunk_rec.get("tactical_context", {})

                # Store chunk metadata as JSONB in the metadata column if available
                cur.execute(
                    "INSERT INTO chunks (source_id, chunk_index, content, embedding) "
                    "VALUES (%s, %s, %s, %s::vector) ON CONFLICT (source_id, chunk_index) DO NOTHING "
                    "RETURNING id",
                    (source_id, idx, content, vec_literal(vec)),
                )
                row = cur.fetchone()
                chunk_id = row[0] if row else None

                # Extract tactical patterns from chunks with sufficient tactical density
                if chunk_id and ctx.get("tactical_density", 0) > 0.1:
                    patterns = extract_tactical_patterns(content, source_id=source_id, chunk_id=chunk_id)
                    all_patterns.extend(patterns)

            # Store tactical patterns
            for p in all_patterns:
                cur.execute(
                    "INSERT INTO tactical_patterns "
                    "(source_id, chunk_id, pattern_type, actor, action, context, zones, phase) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (p["source_id"], p["chunk_id"], p["pattern_type"],
                     p.get("actor"), p["action"], p.get("context", "")[:300],
                     p.get("zones") or [], p.get("phase")),
                )

            conn.commit()
    except Exception as exc:
        conn.rollback()
        log.exception("Chunk embed/store failed for source_id=%s: %s", source_id, exc)
        set_source_embed_status(conn, source_id, "embed_failed", str(exc))
        return

    set_source_embed_status(conn, source_id, "embedded")

    if all_patterns:
        log.info("Extracted %d tactical patterns from source_id=%s", len(all_patterns), source_id)


def _bertrend_lookback_days() -> int:
    bertrend_cfg = _CFG.get("bertrend", {}) if isinstance(_CFG.get("bertrend"), dict) else {}
    try:
        return max(1, int(bertrend_cfg.get("lookback_days", 14)))
    except (TypeError, ValueError):
        return 14


def _count_recent_embedded_chunks(conn, lookback_days: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) "
            "FROM chunks c "
            "JOIN sources s ON s.id = c.source_id "
            "WHERE s.created_at > NOW() - make_interval(days => %s) "
            "AND c.embedding IS NOT NULL",
            (lookback_days,),
        )
        return int(cur.fetchone()[0] or 0)


def _select_sources_missing_embeddings(conn, lookback_days: int, limit: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.id, s.title, s.content, COALESCE(s.metadata->>'embed_status', ''), "
            "COUNT(c.id) FILTER (WHERE c.embedding IS NOT NULL) AS embedded_chunks, "
            "COUNT(c.id) AS total_chunks "
            "FROM sources s "
            "LEFT JOIN chunks c ON c.source_id = s.id "
            "WHERE s.created_at > NOW() - make_interval(days => %s) "
            "GROUP BY s.id, s.title, s.content, s.metadata, s.created_at "
            "HAVING COUNT(c.id) FILTER (WHERE c.embedding IS NOT NULL) = 0 "
            "ORDER BY s.created_at DESC "
            "LIMIT %s",
            (lookback_days, limit),
        )
        return cur.fetchall()


def _reset_source_embeddings(conn, source_id: int):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tactical_patterns WHERE source_id = %s", (source_id,))
        cur.execute("DELETE FROM chunks WHERE source_id = %s", (source_id,))
    conn.commit()


def run_backfill(conn, lookback_days: int = 14, limit: int = 200) -> int:
    candidates = _select_sources_missing_embeddings(conn, lookback_days=lookback_days, limit=limit)
    if not candidates:
        log.info(
            "Backfill skipped: no recent sources missing embeddings (lookback=%dd, limit=%d)",
            lookback_days,
            limit,
        )
        return 0

    log.info(
        "Backfill starting: %d recent sources missing embeddings (lookback=%dd, limit=%d)",
        len(candidates),
        lookback_days,
        limit,
    )
    repaired = 0
    skipped = 0
    failed = 0

    for source_id, title, content, embed_status, embedded_chunks, total_chunks in candidates:
        title_preview = (title or "Untitled source")[:80]
        if not (content or "").strip():
            skipped += 1
            log.warning("Backfill skipping source_id=%s title=%r: empty content", source_id, title_preview)
            set_source_embed_status(conn, source_id, "embed_skipped", "Backfill skipped because source content is empty")
            continue

        log.info(
            "Backfill reprocessing source_id=%s title=%r status=%s embedded_chunks=%s total_chunks=%s",
            source_id,
            title_preview,
            embed_status or "(unset)",
            embedded_chunks,
            total_chunks,
        )
        _reset_source_embeddings(conn, source_id)
        chunk_and_embed(conn, source_id, content)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE embedding IS NOT NULL), COUNT(*) "
                "FROM chunks WHERE source_id = %s",
                (source_id,),
            )
            embedded_after, total_after = cur.fetchone()

        if embedded_after:
            repaired += 1
        else:
            failed += 1
            log.warning(
                "Backfill did not restore embeddings for source_id=%s title=%r (chunks=%s)",
                source_id,
                title_preview,
                total_after,
            )

    log.info(
        "Backfill summary: repaired=%d failed=%d skipped=%d candidates=%d",
        repaired,
        failed,
        skipped,
        len(candidates),
    )
    return repaired

# ══════════════════════════════════════════════
# Hybrid retrieval (semantic + keyword via RRF)
# ══════════════════════════════════════════════

def hybrid_search(conn, query, limit=20):
    qvecs = embed([query])
    if not qvecs:
        log.warning("Hybrid search skipped because query embedding could not be generated")
        return []
    qvec = qvecs[0]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h.chunk_id, h.source_id, h.content, s.title, s.url, h.score "
            "FROM hybrid_search(%s, %s::vector, %s) h "
            "JOIN sources s ON s.id = h.source_id",
            (query, vec_literal(qvec), limit),
        )
        return cur.fetchall()

def chunks_to_context(rows):
    """Format retrieved chunk rows as a JSON context packet."""
    return json.dumps([
        {"chunk_id": cid, "source_id": sid, "content": content,
         "source_title": title, "source_url": url}
        for cid, sid, content, title, url, *_ in rows
    ], indent=2)

# ══════════════════════════════════════════════
# LLM helpers
# ══════════════════════════════════════════════

def _coerce_message_content(content):
    """Normalize SDK response content into the string shape expected by callers."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def ask(system, user, model=None, max_tokens=4096):
    """Standard LLM call — system + user → text."""
    resp = _chat_completion_create(
        model=model or MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return _coerce_message_content(resp.choices[0].message.content)


def ask_thinking(system, user, budget_tokens=10000, max_tokens=16000):
    """Lead agent call with deep reasoning enabled.

    Returns (thinking_text, response_text). thinking_text is empty as reasoning
    is internal to the model.
    """
    resp = _chat_completion_create(
        model=LEAD_MODEL,
        max_tokens=max_tokens,
        reasoning_effort="high",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return "", _coerce_message_content(resp.choices[0].message.content)


def parse_json(text):
    """Extract JSON from a text response.

    Tries in order:
    1. Direct parse of stripped text
    2. Strip markdown code fences then parse
    3. Regex extract of first {...} or [...] block

    Logs the raw payload on failure so the exact bad output is visible.
    """
    if isinstance(text, (dict, list)):
        return text
    if text is None:
        raise ValueError("No valid JSON found in response: empty response")

    stripped = text.strip()

    # 1. Direct parse (handles well-formed responses)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Strip code fences (```json ... ``` or ``` ... ```)
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. Regex-extract first JSON object or array
    block_match = re.search(r"[\[{].*[\]}]", stripped, re.DOTALL)
    if block_match:
        try:
            return json.loads(block_match.group())
        except json.JSONDecodeError:
            pass

    lowered = stripped.lower()
    auth_indicators = ("401", "unauthorized", "authentication", "invalid_api_key", "cf-aig-authorization")
    if any(indicator in lowered for indicator in auth_indicators):
        log.error(
            "LLM response was not JSON because the upstream request appears to have failed authentication "
            "(check CLOUDFLARE_GATEWAY_TOKEN / cf-aig-authorization): %r",
            stripped[:500],
        )
    else:
        log.error("parse_json failed — raw payload: %r", stripped[:500])
    raise ValueError(f"No valid JSON found in response: {stripped[:200]}")

# ══════════════════════════════════════════════
# Pipeline state (persists trend between steps)
# ══════════════════════════════════════════════

def save_state(conn, key, value):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            (key, value),
        )
        conn.commit()

def load_state(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM pipeline_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None

# ══════════════════════════════════════════════
# Trend detection
# ══════════════════════════════════════════════

def _tokenize_feedback_text(text: str) -> list[str]:
    """Return unigrams (>2 chars) and bigrams from text for keyword matching."""
    words = [t for t in re.findall(r"[a-z0-9']+", text.lower()) if len(t) > 2]
    # Include bigrams for better phrase-level matching
    bigrams = [f"{words[i]}_{words[i+1]}" for i in range(len(words) - 1)]
    return words + bigrams


def _load_feedback_keyword_weights(conn) -> dict[str, float]:
    """Load time-decayed keyword weights from historical feedback.

    Recent feedback is weighted more heavily via exponential time decay.
    Bigrams are included alongside unigrams for phrase-level matching.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trend_text, feedback_value, created_at FROM trend_feedback ORDER BY created_at DESC LIMIT 2000"
        )
        rows = cur.fetchall()

    if not rows:
        return {}

    now = datetime.now(UTC) if hasattr(datetime, "now") else datetime.utcnow().replace(tzinfo=UTC)
    half_life_days = 14.0  # feedback half-life: 14 days
    decay_k = 0.693 / half_life_days  # ln(2) / half_life

    weights: dict[str, float] = {}
    for trend_text, feedback, created_at in rows:
        if not trend_text or not feedback:
            continue
        # Time decay: recent feedback counts more
        if created_at and hasattr(created_at, "timestamp"):
            age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
        else:
            age_days = 0.0
        time_weight = math.exp(-decay_k * age_days)  # 1.0 for today, 0.5 at 14 days, 0.25 at 28 days

        for token in set(_tokenize_feedback_text(trend_text)):
            weights[token] = weights.get(token, 0.0) + float(feedback) * time_weight

    return weights


def _load_feedback_embeddings(conn) -> list[tuple[list[float], int]]:
    """Load recent feedback trend texts and their embeddings for semantic matching.

    Returns list of (embedding_vector, feedback_value) tuples.
    Generates embeddings on the fly for feedback trends (batched).
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (trend_text) trend_text, feedback_value
               FROM trend_feedback
               ORDER BY trend_text, created_at DESC
               LIMIT 200"""
        )
        rows = cur.fetchall()

    if not rows:
        return []

    texts = [r[0] for r in rows if r[0]]
    feedbacks = [int(r[1]) for r in rows if r[0]]
    if not texts:
        return []

    # Batch embed all feedback trend texts
    vectors = embed(texts)
    if not vectors:
        return []

    return list(zip(vectors, feedbacks))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _feedback_adjustment_for_trend(
    trend: str,
    keyword_weights: dict[str, float],
    feedback_embeddings: list[tuple[list[float], int]] | None = None,
) -> int:
    """Compute feedback adjustment combining keyword matching + semantic similarity.

    Keyword component: sum of time-decayed keyword weights for matching tokens/bigrams.
    Semantic component: cosine similarity between new trend and past feedback trends,
      weighted by feedback value. Only high-similarity matches (>0.6) contribute.

    Combined adjustment is clamped to [-50, +50].
    """
    adjustment = 0.0

    # --- Keyword component (bigrams weighted 2x) ---
    if keyword_weights:
        tokens = set(_tokenize_feedback_text(trend or ""))
        for token in tokens:
            w = keyword_weights.get(token, 0.0)
            if "_" in token:  # bigram — weight more heavily
                w *= 2.0
            adjustment += w

    # --- Semantic similarity component ---
    if feedback_embeddings and trend:
        trend_vectors = embed([trend])
        if trend_vectors:
            trend_vec = trend_vectors[0]
            semantic_adj = 0.0
            for fb_vec, fb_value in feedback_embeddings:
                sim = _cosine_similarity(trend_vec, fb_vec)
                if sim > 0.6:  # only count meaningful similarity
                    # Scale: sim=0.6 → 0, sim=1.0 → full weight
                    strength = (sim - 0.6) / 0.4
                    semantic_adj += strength * fb_value
            # Semantic component can contribute up to ±25
            adjustment += max(-25.0, min(25.0, semantic_adj))

    return max(-50, min(50, int(round(adjustment))))


def _detect_novel_tactical_patterns(conn, past_topics):
    """Detect novel tactical patterns by comparing recent extractions against baselines.

    This is the second detector alongside BERTrend: instead of looking for growing
    topic clusters, it looks for new structured tactical behaviors (role → action → zone)
    that are semantically distant from what has been seen before.

    Returns list of candidate dicts compatible with the pipeline.
    """
    # Fetch recent tactical patterns (last 7 days)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tp.id, tp.actor, tp.action, tp.context, tp.zones, tp.phase, "
            "tp.source_id, s.title AS source_title, s.url AS source_url "
            "FROM tactical_patterns tp "
            "JOIN sources s ON tp.source_id = s.id "
            "WHERE tp.created_at > NOW() - INTERVAL '7 days' "
            "ORDER BY tp.created_at DESC LIMIT 500"
        )
        recent_patterns = cur.fetchall()

    if not recent_patterns:
        log.info("Tactical patterns: 0 patterns in last 7 days")
        return []

    log.info("Tactical patterns: %d raw patterns in last 7 days", len(recent_patterns))

    # Group patterns by action type to find recurring tactical behaviors
    action_groups = {}
    for row in recent_patterns:
        pat_id, actor, action, context, zones, phase, src_id, src_title, src_url = row
        key = f"{actor} {action}"
        if key not in action_groups:
            action_groups[key] = {
                "actor": actor,
                "action": action,
                "contexts": [],
                "source_ids": set(),
                "source_titles": [],
                "pattern_ids": [],
                "zones": set(),
                "phases": set(),
            }
        group = action_groups[key]
        group["contexts"].append(context[:200] if context else "")
        group["source_ids"].add(src_id)
        if src_title and src_title not in group["source_titles"]:
            group["source_titles"].append(src_title)
        group["pattern_ids"].append(pat_id)
        if zones:
            group["zones"].update(zones)
        if phase:
            group["phases"].add(phase)

    # Filter: only patterns with 2+ sources (corroborated, not noise)
    corroborated = {k: v for k, v in action_groups.items() if len(v["source_ids"]) >= 2}
    log.info(
        "Tactical patterns: %d action groups, %d corroborated (2+ sources)",
        len(action_groups), len(corroborated),
    )
    if not corroborated:
        return []

    # Score novelty for each corroborated pattern
    descriptions = []
    groups_list = []
    for key, group in corroborated.items():
        desc = f"{group['actor']} {group['action']}"
        if group["zones"]:
            desc += f" in {', '.join(list(group['zones'])[:2])}"
        if group["phases"]:
            desc += f" during {list(group['phases'])[0]}"
        descriptions.append(desc)
        groups_list.append((key, group))

    vectors = embed(descriptions)
    if not vectors:
        log.warning("Tactical patterns: embed() returned empty for %d descriptions", len(descriptions))
        return []

    candidates = []
    for (key, group), desc, vec in zip(groups_list, descriptions, vectors):
        novelty = compute_novelty_score(conn, desc, vec, source_count=len(group["source_ids"]))

        # Only promote patterns with meaningful novelty
        if novelty < 0.3:
            continue

        # Build candidate compatible with existing pipeline
        score = int(min(100, novelty * 100))
        candidates.append({
            "trend": desc,
            "reasoning": (
                f"Novel tactical pattern detected: {group['actor']} performing {group['action']} "
                f"across {len(group['source_ids'])} sources. "
                f"Zones: {', '.join(list(group['zones'])[:3]) if group['zones'] else 'unspecified'}. "
                f"Novelty score: {novelty:.2f}."
            ),
            "score": score,
            "source_titles": group["source_titles"][:5],
            "sources": [
                {"source_id": sid, "title": "", "url": ""}
                for sid in list(group["source_ids"])[:5]
            ],
            "novelty_score": novelty,
            "source_diversity": len(group["source_ids"]),
            "pattern_ids": group["pattern_ids"],
            "detection_method": "tactical_pattern",
        })

    candidates.sort(key=lambda c: -c["novelty_score"])
    below_threshold = len(corroborated) - len(candidates)
    log.info(
        "Tactical patterns: %d candidates above novelty threshold (0.3), %d below",
        len(candidates), below_threshold,
    )
    return candidates[:10]


def detect_trends(conn) -> tuple[list[dict], bool]:
    """Detect novel trends using BERTrend + tactical pattern detection + LLM synthesis.

    Two complementary detectors run in sequence:
      1. BERTrend: clusters article embeddings over time, tracks which clusters grow.
         Good at detecting "something is being talked about more" (momentum).
      2. Tactical patterns: extracts structured role→action→zone patterns, scores them
         against historical baselines. Good at detecting "one team is quietly doing
         something new" (novelty).

    Results from both detectors are merged and deduplicated. The combined list
    gives the report pipeline stronger candidates than either detector alone.

    Returns (candidates, had_error) matching the existing pipeline interface.
    """
    cfg_path = ROOT / "config.json"

    # Fetch past report titles to avoid repeats
    with conn.cursor() as cur:
        cur.execute("SELECT title FROM reports ORDER BY created_at DESC LIMIT 10")
        past = [r[0] for r in cur.fetchall()]

    all_candidates = []

    # ── Detector 1: BERTrend algorithmic detection ───────────────────────────
    try:
        signals = run_bertrend_detection(conn, cfg_path=cfg_path)
        if signals:
            log.info("BERTrend detected %d signals (%d weak, %d strong)",
                     len(signals),
                     sum(1 for s in signals if s["signal_class"] == "weak"),
                     sum(1 for s in signals if s["signal_class"] == "strong"))

            # Use LLM to synthesize signals into trend descriptions
            candidates = describe_signals_with_llm(
                conn, signals,
                lambda sys, usr: ask(sys, usr, model=SIGNAL_MODEL),
                past_topics=past,
            )
            if candidates:
                for c in candidates:
                    c["detection_method"] = "bertrend"
                log.info("BERTrend + LLM produced %d trend candidates", len(candidates))
                all_candidates.extend(candidates)
        else:
            log.info("BERTrend found no non-noise signals")

    except Exception as e:
        log.warning("BERTrend detection failed (%s): %s", type(e).__name__, e, exc_info=True)

    # ── Detector 2: Tactical pattern novelty detection ───────────────────────
    try:
        pattern_candidates = _detect_novel_tactical_patterns(conn, past)
        if pattern_candidates:
            log.info("Tactical pattern detector found %d novel candidates", len(pattern_candidates))
            all_candidates.extend(pattern_candidates)
    except Exception as e:
        log.warning("Tactical pattern detection failed (%s): %s", type(e).__name__, e, exc_info=True)

    # ── Merge and deduplicate ────────────────────────────────────────────────
    if all_candidates:
        # Deduplicate by checking semantic overlap between candidates
        seen_trends = set()
        deduped = []
        for c in sorted(all_candidates, key=lambda x: -x.get("score", 0)):
            trend_lower = c["trend"].lower().strip()
            # Simple dedup: skip if a very similar trend text already exists
            is_dupe = False
            for seen in seen_trends:
                # Check word overlap
                words_new = set(trend_lower.split())
                words_seen = set(seen.split())
                if len(words_new & words_seen) / max(1, len(words_new | words_seen)) > 0.6:
                    is_dupe = True
                    break
            if not is_dupe:
                seen_trends.add(trend_lower)
                deduped.append(c)
        all_candidates = deduped
        log.info("After deduplication: %d unique candidates", len(all_candidates))

    if all_candidates:
        return all_candidates, False

    # ── Fallback: LLM-only detection (original approach) ─────────────────────
    log.info("Both detectors returned nothing, falling back to LLM-only detection")
    return _detect_trends_llm_only(conn, past)


def _detect_trends_llm_only(conn, past) -> tuple[list[dict], bool]:
    """Original LLM-only trend detection as fallback."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, url, LEFT(content, 500) FROM sources "
            "WHERE created_at > NOW() - INTERVAL '7 days' ORDER BY created_at DESC LIMIT 100"
        )
        recent = cur.fetchall()
    if not recent:
        log.info("LLM-only fallback: 0 sources in last 7 days, nothing to analyze")
        return [], False

    log.info("LLM-only fallback: %d sources in last 7 days", len(recent))

    source_catalog: dict[str, list[dict]] = {}
    normalized_catalog: dict[str, list[dict]] = {}

    def _normalize_title(value: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())

    summaries = []
    for source_id, title, url, content in recent:
        source_title = (title or "Untitled source").strip()
        normalized_title = _normalize_title(source_title)
        summaries.append(f"- {source_title}: {content}...")
        source_catalog.setdefault(source_title, []).append(
            {
                "source_id": source_id,
                "title": source_title,
                "url": url or "",
            }
        )
        normalized_catalog.setdefault(normalized_title, []).append(
            {
                "source_id": source_id,
                "title": source_title,
                "url": url or "",
            }
        )

    past_block = "\n".join(f"- {t}" for t in past) if past else "(none)"

    prompt_body = "Recent articles and transcripts:\n" + "\n".join(summaries) + "\n\n"
    log.info(
        "LLM-only fallback: sending %d sources, prompt ~%d chars, %d past topics excluded",
        len(recent), len(prompt_body), len(past),
    )

    try:
        text = ask(
            "You are a football tactics analyst spotting novel trends before they go mainstream.",
            prompt_body +
            f"Already-covered topics (avoid repeating):\n{past_block}\n\n"
            "Identify the top 5 most novel tactical or strategic trends being tried by football "
            "players or teams. Rank them by novelty — things not yet widely adopted get higher scores.\n\n"
            "Score each trend 0-100 where 100 = extremely novel and underreported, 0 = widely known.\n\n"
            "For each trend include source_titles as a list of exact titles from the provided source list that most strongly support the trend.\n\n"
            "Return ONLY valid JSON. No markdown. No code fences. No prose. Use double quotes.\n"
            'Format: {"candidates": ['
            '{"trend": "<10-20 word description>", "reasoning": "<why novel>", "score": <0-100>, "source_titles": ["<exact title>"]}'
            ', ...]}'
        )
        log.info("LLM-only trend detection raw response: %r", text)
        candidates = parse_json(text).get("candidates", [])
        valid = []
        for c in candidates:
            if not (isinstance(c, dict) and c.get("trend") and isinstance(c.get("score"), int)):
                continue

            matched_sources = []
            for title in c.get("source_titles") or []:
                query_title = str(title).strip()
                query_normalized = _normalize_title(query_title)

                matched_sources.extend(source_catalog.get(query_title, []))
                matched_sources.extend(normalized_catalog.get(query_normalized, []))

                if query_normalized:
                    for known_normalized, known_sources in normalized_catalog.items():
                        if query_normalized in known_normalized or known_normalized in query_normalized:
                            matched_sources.extend(known_sources)

            deduped = []
            seen_source_ids = set()
            for source in matched_sources:
                if source["source_id"] in seen_source_ids:
                    continue
                seen_source_ids.add(source["source_id"])
                deduped.append(source)

            c["sources"] = deduped
            valid.append(c)

        log.info(
            "LLM-only fallback: %d raw candidates from LLM, %d valid after filtering",
            len(candidates), len(valid),
        )
        return valid, False
    except Exception as e:
        log.warning("LLM-only trend detection failed: %s", e, exc_info=True)
        return [], True

# ══════════════════════════════════════════════
# Step 1: LeadResearcher — decompose with extended thinking + effort scaling
# ══════════════════════════════════════════════

def decompose_topic(trend):
    """Lead agent uses extended thinking to reason about decomposition strategy.

    Implements Anthropic's effort scaling: the lead agent assesses complexity
    and calibrates the number of subagents and retrieval depth accordingly.
    """
    thinking, response = ask_thinking(
        SYS_DECOMPOSE,

        f"Research topic: {trend}\n\n"
        "Think step by step:\n"
        "1. What is the complexity level of this topic?\n"
        "2. What are the distinct, non-overlapping research angles?\n"
        "3. What search queries would each angle need (broad first, then narrow)?\n"
        "4. What boundaries prevent duplication between angles?\n\n"
        "Return JSON:\n"
        "```json\n"
        '{\n'
        '  "complexity": "simple|moderate|complex",\n'
        '  "reasoning": "why this complexity level",\n'
        '  "tasks": [\n'
        '    {\n'
        '      "angle": "short name",\n'
        '      "objective": "what this subagent must find and analyze",\n'
        '      "search_queries": ["broad query first", "narrower query", "specific query"],\n'
        '      "boundaries": "what is explicitly OUT of scope for this subagent",\n'
        '      "max_rounds": 3\n'
        '    }\n'
        '  ]\n'
        '}\n'
        "```",
        budget_tokens=10000,
    )
    log.info("Lead agent thinking: %s...", thinking[:200] if thinking else "(none)")

    data = parse_json(response)
    tasks = data.get("tasks", data if isinstance(data, list) else [data])
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks") or [tasks]

    complexity = data.get("complexity", "moderate")
    log.info("Lead agent: complexity=%s, %d angles: %s",
             complexity, len(tasks), [t.get("angle") for t in tasks])
    return tasks, complexity

# ══════════════════════════════════════════════
# Step 2: Subagent — OODA retrieval loop (broad-to-narrow)
# ══════════════════════════════════════════════

def research_angle(conn, trend, task):
    """Subagent with OODA loop: Observe → Orient → Decide → Act.

    Mirrors Anthropic's subagent pattern:
    - Start with broad queries to survey the landscape
    - Orient: evaluate what was found vs what's needed
    - Decide: generate a narrower, more targeted query
    - Act: retrieve again with refined query
    - Repeat until sufficient or max rounds reached
    """
    angle = task.get("angle", "general")
    objective = task.get("objective", "")
    queries = list(task.get("search_queries", [f"{trend} {angle}"]))
    boundaries = task.get("boundaries", "")
    max_rounds = task.get("max_rounds", 3)
    all_chunks = {}  # chunk_id -> row, deduplicated

    for round_num in range(max_rounds):
        # ACT: retrieve with current query
        query = queries[round_num] if round_num < len(queries) else queries[-1]
        log.info("  Subagent '%s' round %d/%d: query='%s'", angle, round_num + 1, max_rounds, query[:60])
        rows = hybrid_search(conn, query, limit=15)
        for row in rows:
            all_chunks[row[0]] = row

        if not all_chunks:
            continue

        # OBSERVE + ORIENT: evaluate what we have vs what we need
        chunk_json = chunks_to_context(list(all_chunks.values()))
        try:
            eval_text = ask(
                SYS_OODA_EVAL,

                f"Angle: {angle}\n"
                f"Objective: {objective}\n"
                f"Round: {round_num + 1}/{max_rounds}\n"
                f"Previous queries: {json.dumps(queries[:round_num + 1])}\n\n"
                f"Chunks collected ({len(all_chunks)} total):\n{chunk_json}\n\n"
                'Return JSON: {{"sufficient": true/false, "coverage_pct": 0-100, '
                '"gaps": ["specific gap 1", ...], "next_query": "narrower query" or null}}',
                model=EVAL_MODEL,
            )
            eval_result = parse_json(eval_text)
        except Exception:
            break

        coverage = eval_result.get("coverage_pct", 0)
        log.info("  Subagent '%s' coverage: %d%%, sufficient: %s",
                 angle, coverage, eval_result.get("sufficient"))

        if eval_result.get("sufficient", False) or round_num == max_rounds - 1:
            break

        # DECIDE: use narrower query for next round
        next_q = eval_result.get("next_query")
        if next_q:
            queries.append(next_q)

    last_coverage = eval_result.get("coverage_pct", 50) if 'eval_result' in dir() else 50

    if not all_chunks:
        return {"angle": angle, "summary": f"No evidence found for: {angle}",
                "chunks": [], "coverage": 0}

    chunk_json = chunks_to_context(list(all_chunks.values()))

    # Write grounded summary for this angle
    summary = ask(
        SYS_SUBAGENT,

        f"Angle: {angle}\n"
        f"Objective: {objective}\n"
        f"Out of scope: {boundaries}\n\n"
        f"Evidence chunks:\n{chunk_json}\n\n"
        "Write a thorough, evidence-grounded analysis for this angle:\n"
        "- Lead with the strongest finding\n"
        "- Use inline citations [S<source_id>:C<chunk_id>] on every claim\n"
        "- Bold key statistics and figures\n"
        "- Note evidence quality and any limitations\n"
        "- Flag if evidence was insufficient for any part of the objective",
        model=SUMMARY_MODEL,
    )
    log.info("Subagent '%s' done: %d chunks, %d rounds", angle, len(all_chunks), round_num + 1)
    return {"angle": angle, "summary": summary, "chunks": list(all_chunks.values()),
            "coverage": last_coverage}

def run_subagents(conn, trend, tasks):
    """Run subagent research in parallel with bounded concurrency."""
    results = []
    with ThreadPoolExecutor(max_workers=min(len(tasks), 4)) as pool:
        futures = {pool.submit(research_angle, conn, trend, task): task for task in tasks}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                task = futures[future]
                log.warning("Subagent '%s' failed: %s", task.get("angle"), e)
                results.append({"angle": task.get("angle", "?"),
                                "summary": f"Research failed: {e}", "chunks": [], "coverage": 0})
    return results

# ══════════════════════════════════════════════
# Step 3: Synthesis — merge subagent outputs
# ══════════════════════════════════════════════

def collect_all_chunks(subagent_results):
    """Deduplicate chunks across all subagent results."""
    all_chunks = {}
    for r in subagent_results:
        for row in r["chunks"]:
            all_chunks[row[0]] = row
    return list(all_chunks.values())

def synthesize(trend, subagent_results):
    """Merge parallel subagent summaries into a cohesive draft report."""
    summaries_text = "\n\n---\n\n".join(
        f"### Angle: {r['angle']} (coverage: {r.get('coverage', '?')}%)\n\n{r['summary']}"
        for r in subagent_results
    )
    all_chunks = collect_all_chunks(subagent_results)
    chunk_json = chunks_to_context(all_chunks)

    weak = [r["angle"] for r in subagent_results if r.get("coverage", 100) < 40]
    failed = [r["angle"] for r in subagent_results if not r["chunks"]]

    draft = ask(
        SYS_SYNTHESIS,

        f"Topic: {trend}\n\n"
        f"Subagent summaries:\n{summaries_text}\n\n"
        f"All deduplicated evidence chunks ({len(all_chunks)} total):\n{chunk_json}\n\n"
        f"Failed angles (no evidence): {', '.join(failed) if failed else '(none)'}\n"
        f"Weak angles (<40% coverage): {', '.join(weak) if weak else '(none)'}\n\n"
        "Produce a comprehensive markdown report:\n"
        "# [Descriptive Title]\n\n"
        "## Executive Summary\n"
        "Concise overview of the trend, why it matters, and key findings.\n\n"
        "## Key Findings\n"
        "Numbered list of the most important findings with evidence.\n\n"
        "## [Angle-specific H2 sections]\n"
        "One H2 per research angle with H3 subsections where depth warrants it.\n"
        "Cross-reference between angles where findings connect.\n\n"
        "## Evidence Assessment\n"
        "Overall quality, limitations, failed/weak angles acknowledged.\n\n"
        "## Implications\n"
        "What this means for football tactics going forward.\n\n"
        "## Open Questions\n"
        "What remains unknown or under-evidenced.\n\n"
        "## Sources\n"
        "All cited sources with titles and URLs.\n\n"
        "Requirements:\n"
        "- Every claim must have inline citation [S<source_id>:C<chunk_id>]\n"
        "- **Bold** key statistics and figures\n"
        "- Tables for structured comparisons where useful\n"
        "- `---` separators between major sections\n"
        "- Flag any speculation explicitly\n"
        "- Acknowledge evidence gaps honestly",
        model=SYNTHESIS_MODEL,
        max_tokens=12000,
    )
    return draft, chunk_json, all_chunks

# ══════════════════════════════════════════════
# Step 4: Sufficiency evaluation — lead agent re-planning loop
# ══════════════════════════════════════════════

def evaluate_sufficiency(trend, draft, subagent_results, chunk_json):
    """Lead agent evaluates if the draft is sufficient or needs more research.

    This is the re-planning loop from Anthropic's architecture: after synthesis,
    the LeadResearcher decides whether to spawn additional subagents for gaps.
    """
    coverage_summary = "\n".join(
        f"- {r['angle']}: {r.get('coverage', '?')}% coverage, {len(r['chunks'])} chunks"
        for r in subagent_results
    )

    thinking, response = ask_thinking(
        SYS_SUFFICIENCY,

        f"Topic: {trend}\n\n"
        f"Subagent coverage:\n{coverage_summary}\n\n"
        f"Draft report:\n{draft}\n\n"
        "Evaluate:\n"
        "1. Are there critical evidence gaps that undermine the report's credibility?\n"
        "2. Are any angles so weak they need additional retrieval?\n"
        "3. Did the draft reveal a NEW angle not in the original decomposition?\n\n"
        "Return JSON:\n"
        '{"sufficient": true/false, "gaps": [{"angle": "...", "objective": "...", '
        '"search_queries": ["..."], "boundaries": "...", "max_rounds": 2}]}',
        budget_tokens=8000,
    )
    log.info("Sufficiency thinking: %s...", thinking[:200] if thinking else "(none)")

    try:
        result = parse_json(response)
    except Exception:
        return True, []

    return result.get("sufficient", True), result.get("gaps", [])

# ══════════════════════════════════════════════
# Step 5: CitationAgent — dedicated citation verification
# ══════════════════════════════════════════════

def verify_citations(trend, draft, chunk_json):
    """Dedicated CitationAgent that verifies every citation maps to real evidence.

    Matches Anthropic's architecture where a separate CitationAgent processes
    documents and the research report to identify specific locations for citations.
    """
    return ask(
        SYS_CITATION,

        f"Topic: {trend}\n\n"
        f"Available source chunks:\n{chunk_json}\n\n"
        f"Report to verify:\n{draft}\n\n"
        "Return a structured verification report:\n\n"
        "## Citation Verification Summary\n"
        "Total citations found, valid count, invalid count.\n\n"
        "## Invalid Citations\n"
        "List each invalid citation with:\n"
        "- The exact citation tag\n"
        "- The claim it's attached to\n"
        "- Why it's invalid (non-existent ID, claim not supported, wrong chunk)\n"
        "- Suggested fix (correct chunk ID, remove claim, or add qualifier)\n\n"
        "## Uncited Claims\n"
        "Claims that make factual assertions without citations.\n"
        "For each, suggest the correct chunk to cite or flag for removal.\n\n"
        "## Sources Section Errors\n"
        "Any sources listed that weren't cited, or cited sources not listed.\n\n"
        "## Revision Directives\n"
        "Ordered list of specific changes for the revision editor.",
        model=CITATION_MODEL,
    )

# ══════════════════════════════════════════════
# Step 6: Revision — final report incorporating all feedback
# ══════════════════════════════════════════════

def revise(trend, draft, citation_report, chunk_json):
    """Produce the final report incorporating citation verification feedback."""
    return ask(
        SYS_REVISION,

        f"Topic: {trend}\n\n"
        f"Source chunks:\n{chunk_json}\n\n"
        f"Draft report:\n{draft}\n\n"
        f"Citation verification report:\n{citation_report}\n\n"
        "Produce the final revised markdown report:\n"
        "1. Fix every invalid citation identified by the CitationAgent\n"
        "2. Add citations to every uncited factual claim (using correct chunk IDs)\n"
        "3. Remove or qualify claims where no supporting chunk exists\n"
        "4. Fix the Sources section to match actual citations\n"
        "5. Preserve all well-grounded claims and their citations\n"
        "6. Maintain the full report structure:\n"
        "   # Title\n"
        "   ## Executive Summary\n"
        "   ## Key Findings (numbered)\n"
        "   ## [Angle-specific sections with H3 subsections]\n"
        "   ## Evidence Assessment\n"
        "   ## Implications\n"
        "   ## Open Questions\n"
        "   ## Sources\n"
        "7. **Bold** key statistics, use tables where appropriate\n"
        "8. Explicitly flag remaining speculation with qualifiers like "
        "\"evidence suggests\" or \"it appears that\"\n"
        "9. Use `---` separators between major sections",
        model=REVISION_MODEL,
        max_tokens=12000,
    )

# ══════════════════════════════════════════════
# Orchestration: full multi-agent pipeline with re-planning
# ══════════════════════════════════════════════

MAX_RESEARCH_ROUNDS = 2  # max re-planning iterations

def generate_report(conn, trend):
    """Full pipeline matching Anthropic's multi-agent research architecture.

    LeadResearcher (extended thinking, effort scaling)
      → Parallel Subagents (OODA retrieval, broad-to-narrow)
      → Synthesis
      → Sufficiency evaluation (re-planning loop)
      → CitationAgent (dedicated verification)
      → Revision
    """

    # ── Step 1: Lead agent decomposes with extended thinking ──
    log.info("Step 1: LeadResearcher decomposing topic with extended thinking...")
    tasks, complexity = decompose_topic(trend)

    all_subagent_results = []

    for research_round in range(MAX_RESEARCH_ROUNDS):
        # ── Step 2: Parallel subagent research (OODA retrieval) ──
        round_label = f"Round {research_round + 1}"
        log.info("Step 2 (%s): Running %d subagents in parallel...", round_label, len(tasks))
        results = run_subagents(conn, trend, tasks)
        all_subagent_results.extend(results)

        # ── Step 3: Synthesis ──
        log.info("Step 3 (%s): Synthesizing %d subagent outputs...", round_label, len(all_subagent_results))
        draft, chunk_json, all_chunks = synthesize(trend, all_subagent_results)

        # ── Step 4: Sufficiency evaluation (re-planning) ──
        if research_round < MAX_RESEARCH_ROUNDS - 1:
            log.info("Step 4 (%s): LeadResearcher evaluating sufficiency...", round_label)
            sufficient, gap_tasks = evaluate_sufficiency(trend, draft, all_subagent_results, chunk_json)
            if sufficient or not gap_tasks:
                log.info("LeadResearcher: research sufficient, proceeding to citation verification")
                break
            log.info("LeadResearcher: found %d gaps, spawning additional subagents", len(gap_tasks))
            tasks = gap_tasks  # next round researches the gaps
        else:
            log.info("Max research rounds reached, proceeding to citation verification")

    # ── Step 5: CitationAgent ──
    log.info("Step 5: CitationAgent verifying citations...")
    citation_report = verify_citations(trend, draft, chunk_json)

    # ── Step 6: Revision ──
    log.info("Step 6: Final revision incorporating citation feedback...")
    final_report = revise(trend, draft, citation_report, chunk_json)

    # ── Save ──
    metadata = json.dumps({
        "complexity": complexity,
        "angles": [r["angle"] for r in all_subagent_results],
        "total_chunks": len(all_chunks),
        "research_rounds": research_round + 1,
        "models": {
            "lead": LEAD_MODEL,
            "eval": EVAL_MODEL,
            "summary": SUMMARY_MODEL,
            "synthesis": SYNTHESIS_MODEL,
            "citation": CITATION_MODEL,
            "revision": REVISION_MODEL,
            "signal": SIGNAL_MODEL,
        },
    })
    with conn.cursor() as cur:
        cur.execute("INSERT INTO reports (title, content, metadata) VALUES (%s, %s, %s::jsonb)",
                    (trend, final_report, metadata))
        conn.commit()

    slug = re.sub(r"[^a-z0-9]+", "-", trend.lower()).strip("-")[:60]
    out = ROOT / "reports" / f"{datetime.now().strftime('%Y-%m-%d')}-{slug}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(final_report)
    log.info("Report saved: %s (%d chunks, %d angles, %d rounds)",
             out, len(all_chunks), len(all_subagent_results), research_round + 1)
    return final_report


def _connect_db():
    conninfo, reason = resolve_database_conninfo()
    if not conninfo:
        if reason == "missing_hostname":
            log.error("A Postgres URL env var is set but does not include a hostname, so psycopg falls back to a local unix socket.")
            log.error("In Railway, use a Postgres reference variable (for example: ${{pgvector.DATABASE_URL}} or ${{pgvector.DATABASE_PRIVATE_URL}}) or provide PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE.")
        else:
            log.error("No usable Postgres connection config found.")
            log.error("Set DATABASE_URL/DATABASE_PRIVATE_URL/DATABASE_PUBLIC_URL to a full managed Postgres URL, or provide PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE.")
        raise SystemExit(2)

    try:
        return psycopg.connect(conninfo)
    except psycopg.OperationalError as e:
        log.error("Failed to connect to Postgres: %s", e)
        log.error("Check DATABASE_URL/PG* variables in Railway and ensure they reference your Postgres service.")
        raise SystemExit(2) from e


def _ensure_schema(conn):
    schema_path = ROOT / "sql" / "schema.sql"
    with conn.cursor() as cur:
        cur.execute(schema_path.read_text())
    conn.commit()


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

def run_ingest(conn):
    new = 0
    candidates_found = 0
    articles_extracted = 0
    duplicates = 0
    skipped = 0
    youtube_discovery_failures = 0
    youtube_counters = {
        "youtube_discovery_successes": 0,
        "youtube_discovery_hard_denies": 0,
        "youtube_discovery_retryable_failures": 0,
        "youtube_transcript_successes": 0,
        "youtube_transcript_failures": 0,
    }

    normalize_youtube_source_config(ROOT / "feeds" / "youtube.md")

    # Determine since_ts so NewsBlur only returns stories newer than the last run.
    since_ts = None
    last_completed = load_state(conn, "last_ingest_completed_at")
    if last_completed:
        try:
            dt = datetime.fromisoformat(last_completed)
            since_ts = dt.timestamp()
            log.info("Fetching NewsBlur stories newer_than=%s (%s)", int(since_ts), last_completed)
        except Exception as e:
            log.warning("Could not parse last_ingest_completed_at %r: %s — fetching all stories", last_completed, e)

    for item in fetch_newsblur(since_ts=since_ts):
        candidates_found += 1
        articles_extracted += 1
        dedupe_key = item["key"]
        canonical_url = canonicalize_url(item.get("url", ""))
        existed = source_exists_by_key(conn, dedupe_key)
        sid = store_source(conn, item, "rss")
        if sid:
            log.info("Ingest decision=new source_type=rss dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)
            chunk_and_embed(conn, sid, item["content"])
            new += 1
        elif existed:
            duplicates += 1
            log.info("Ingest decision=duplicate source_type=rss dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)
        else:
            skipped += 1
            log.info("Ingest decision=skipped source_type=rss dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)

    for name, cid in parse_youtube(ROOT / "feeds" / "youtube.md"):
        yt_items, discovery_failed, counters = fetch_youtube(name, cid)
        for key, value in counters.items():
            youtube_counters[key] += value
        if discovery_failed:
            youtube_discovery_failures += 1
            continue
        for item in yt_items:
            candidates_found += 1
            dedupe_key = item["key"]
            canonical_url = canonicalize_url(item.get("url", ""))
            existed = source_exists_by_key(conn, dedupe_key)
            sid = store_source(conn, item, "youtube")
            if sid:
                log.info("Ingest decision=new source_type=youtube dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)
                chunk_and_embed(conn, sid, item["content"])
                new += 1
            elif existed:
                duplicates += 1
                log.info("Ingest decision=duplicate source_type=youtube dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)
            else:
                skipped += 1
                log.info("Ingest decision=skipped source_type=youtube dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)

    save_state(conn, "last_ingest_new_sources", str(new))
    save_state(conn, "last_ingest_completed_at", datetime.now(UTC).isoformat())
    log.info(
        "Ingest summary: candidates_found=%d articles_extracted=%d duplicates=%d new_inserts=%d skipped=%d youtube_discovery_failures=%d youtube_discovery_successes=%d youtube_discovery_hard_denies=%d youtube_discovery_retryable_failures=%d youtube_transcript_successes=%d youtube_transcript_failures=%d",
        candidates_found,
        articles_extracted,
        duplicates,
        new,
        skipped,
        youtube_discovery_failures,
        youtube_counters["youtube_discovery_successes"],
        youtube_counters["youtube_discovery_hard_denies"],
        youtube_counters["youtube_discovery_retryable_failures"],
        youtube_counters["youtube_transcript_successes"],
        youtube_counters["youtube_transcript_failures"],
    )
    log.info("Ingested %d new sources", new)
    return new


def run_detect(conn, min_new_sources=0, backfill_days: int | None = None, backfill_limit: int = 200):
    if min_new_sources > 0:
        latest_new = load_state(conn, "last_ingest_new_sources")
        try:
            latest_new_count = int(latest_new) if latest_new is not None else 0
        except ValueError:
            latest_new_count = 0
        if latest_new_count < min_new_sources:
            log.info(
                "Skipping detect: only %d new sources in latest ingest (min required: %d)",
                latest_new_count,
                min_new_sources,
            )
            return

    lookback_days = backfill_days or _bertrend_lookback_days()
    recent_embedded_chunks = _count_recent_embedded_chunks(conn, lookback_days)
    if recent_embedded_chunks == 0:
        log.warning(
            "Detect found 0 recent embedded chunks for BERTrend lookback=%dd. Triggering backfill.",
            lookback_days,
        )
        run_backfill(conn, lookback_days=lookback_days, limit=backfill_limit)

    candidates, had_error = detect_trends(conn)
    if had_error:
        log.error(
            "Trend detection run failed due to response-format/parsing error "
            "(candidates returned: %d)", len(candidates),
        )
        raise SystemExit(1)
    if candidates:
        keyword_weights = _load_feedback_keyword_weights(conn)
        feedback_embeddings = _load_feedback_embeddings(conn)
        if feedback_embeddings:
            log.info("Loaded %d feedback embeddings for semantic matching", len(feedback_embeddings))

        # ── Novelty scoring for candidates that don't already have it ────────
        candidates_needing_novelty = [c for c in candidates if "novelty_score" not in c]
        if candidates_needing_novelty:
            novelty_texts = [c["trend"] for c in candidates_needing_novelty]
            novelty_vecs = embed(novelty_texts)
            if novelty_vecs:
                for c, vec in zip(candidates_needing_novelty, novelty_vecs):
                    src_count = len(c.get("sources") or [])
                    c["novelty_score"] = compute_novelty_score(conn, c["trend"], vec, source_count=src_count)
                    c["_embedding"] = vec  # stash for baseline update

        with conn.cursor() as cur:
            stored_scores = []
            for c in candidates:
                feedback_adjustment = _feedback_adjustment_for_trend(c["trend"], keyword_weights, feedback_embeddings)
                base_score = int(c["score"])
                novelty = c.get("novelty_score")

                # Novelty bonus/penalty: high novelty boosts score, low novelty suppresses
                novelty_adjustment = 0
                if novelty is not None:
                    # Scale: novelty 0.0 → -15, novelty 0.5 → 0, novelty 1.0 → +15
                    novelty_adjustment = int(round((novelty - 0.5) * 30))

                final_score = max(0, min(100, base_score + feedback_adjustment + novelty_adjustment))
                source_diversity = c.get("source_diversity", len(c.get("sources") or []))

                cur.execute(
                    "INSERT INTO trend_candidates "
                    "(trend, reasoning, score, feedback_adjustment, final_score, "
                    "novelty_score, source_diversity) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (c["trend"], c.get("reasoning"), base_score, feedback_adjustment,
                     final_score, novelty, source_diversity),
                )
                trend_candidate_id = cur.fetchone()[0]
                for source in c.get("sources") or []:
                    cur.execute(
                        "INSERT INTO trend_candidate_sources (trend_candidate_id, source_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (trend_candidate_id, source["source_id"]),
                    )
                stored_scores.append(final_score)

                # Update novelty baseline so future similar concepts register as less novel
                trend_vec = c.get("_embedding")
                if trend_vec:
                    update_baseline(conn, c["trend"], trend_vec, source_count=source_diversity)

        conn.commit()
        log.info("Stored %d trend candidates (top final score: %d)", len(candidates), max(stored_scores))
    else:
        log.info("No novel trends detected this run")


def run_report(conn):
    """Select the top pending candidate that clears a quality gate, then generate a report.

    Quality gate:
      - final_score >= 40 (meaningful signal, not noise)
      - source_diversity >= 2 (corroborated by multiple sources, not an outlier)

    Candidates below the gate are skipped rather than wasting expensive report
    generation on weak or single-source signals.
    """
    min_score = int(_CFG.get("report_min_score", 40))
    min_sources = int(_CFG.get("report_min_sources", 2))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, trend, COALESCE(final_score, score) AS effective_score, "
            "COALESCE(source_diversity, 0) AS src_div "
            "FROM trend_candidates WHERE status = 'pending' "
            "ORDER BY COALESCE(final_score, score) DESC, detected_at DESC LIMIT 5"
        )
        rows = cur.fetchall()

    if not rows:
        log.info("No pending trend candidates — skipping report")
        return

    # Find the best candidate that passes the quality gate
    chosen = None
    skipped_ids = []
    for row in rows:
        cid, trend, eff_score, src_div = row
        if eff_score >= min_score and src_div >= min_sources:
            chosen = (cid, trend, eff_score, src_div)
            break
        else:
            log.info("Skipping candidate '%s' (score=%d, sources=%d) — below quality gate",
                     trend[:60], eff_score, src_div)
            skipped_ids.append(cid)

    # Skip candidates that didn't meet the gate
    if skipped_ids:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(skipped_ids))
            cur.execute(
                f"UPDATE trend_candidates SET status = 'skipped' WHERE id IN ({placeholders})",
                skipped_ids,
            )
        conn.commit()

    if not chosen:
        log.info("No candidates passed the quality gate (min_score=%d, min_sources=%d)",
                 min_score, min_sources)
        return

    candidate_id, trend, eff_score, src_div = chosen
    log.info("Generating report for trend: %s (score=%d, sources=%d)", trend, eff_score, src_div)
    generate_report(conn, trend)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE trend_candidates SET status = 'reported' WHERE id = %s",
            (candidate_id,),
        )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Football research pipeline")
    parser.add_argument(
        "--step",
        choices=["ingest", "backfill", "detect", "report", "all"],
        default="ingest",
        help="Pipeline step to run (default: ingest)",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=_bertrend_lookback_days(),
        help="Days of recent sources to scan for missing embeddings during backfill (default: bertrend.lookback_days)",
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        default=200,
        help="Maximum number of sources to reprocess during backfill (default: 200)",
    )
    parser.add_argument(
        "--min-new-sources-for-detect",
        type=int,
        default=0,
        help="Skip detect when latest ingest inserted fewer than this many new sources (default: 0)",
    )
    parser.add_argument(
        "--allow-report-after-detect",
        action="store_true",
        help="When using --step all, also run report in the same process (disabled by default)",
    )
    args = parser.parse_args()

    _validate_required_env(args.step)

    conn = _connect_db()
    try:
        _ensure_schema(conn)
        if args.step == "ingest":
            run_ingest(conn)
        elif args.step == "backfill":
            run_backfill(conn, lookback_days=args.backfill_days, limit=args.backfill_limit)
        elif args.step == "detect":
            run_detect(
                conn,
                min_new_sources=args.min_new_sources_for_detect,
                backfill_days=args.backfill_days,
                backfill_limit=args.backfill_limit,
            )
        elif args.step == "report":
            run_report(conn)
        else:
            run_ingest(conn)
            run_detect(
                conn,
                min_new_sources=args.min_new_sources_for_detect,
                backfill_days=args.backfill_days,
                backfill_limit=args.backfill_limit,
            )
            if args.allow_report_after_detect:
                run_report(conn)
            else:
                log.info(
                    "Step 'all' now runs ingest+detect only. "
                    "Use --allow-report-after-detect to run report in the same process."
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
