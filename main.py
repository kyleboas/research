#!/usr/bin/env python3
"""Football research pipeline: ingest → detect trends → multi-agent deep research report.

Architecture mirrors Anthropic's production research system:
  LeadResearcher (extended thinking) → parallel Subagents (OODA retrieval)
  → Synthesis → Sufficiency evaluation → optional re-plan → CitationAgent → Revision
"""

import argparse, base64, hashlib, json, logging, math, os, random, re, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import openai, psycopg
from dotenv import load_dotenv
from db_conn import resolve_database_conninfo
from detect_policy import compute_final_score, passes_report_gate
from detect_detectors import (
    detect_novel_tactical_patterns as detect_novel_tactical_patterns_impl,
    detect_trends as detect_trends_impl,
    detect_trends_llm_only as detect_trends_llm_only_impl,
)
from detect_orchestration import run_detect as run_detect_impl, run_rescore as run_rescore_impl
from detect_persistence import (
    effective_source_diversity as effective_source_diversity_impl,
    normalize_trend_text as normalize_trend_text_impl,
    parse_rescore_statuses as parse_rescore_statuses_impl,
    rescored_trend_candidate_values as rescored_trend_candidate_values_impl,
    trend_fingerprint as trend_fingerprint_impl,
    upsert_trend_candidate as upsert_trend_candidate_impl,
)
from detect_scoring import (
    feedback_adjustment_for_trend as feedback_adjustment_for_trend_impl,
    load_feedback_embeddings as load_feedback_embeddings_impl,
    load_feedback_keyword_weights as load_feedback_keyword_weights_impl,
    tokenize_feedback_text as tokenize_feedback_text_impl,
)
from trend_detection import run_bertrend_detection, describe_signals_with_llm
from article_extractor import extract_article, should_extract
from tactical_extraction import chunk_with_context, extract_tactical_patterns, extract_tactical_context
from novelty_scoring import update_baseline
from ingest_policy import load_policy as load_ingest_policy
from report_policy import load_policy as load_report_policy
from runtime_logging import finish_run, llm_usage_tracking, record_llm_usage, start_run, utc_now

log = logging.getLogger("research")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# ── Cloudflare AI Gateway ──────────────────────────────────────────────────────
CLOUDFLARE_GATEWAY_URL = os.environ.get("CLOUDFLARE_GATEWAY_URL", "")
CLOUDFLARE_GATEWAY_TOKEN = os.environ.get("CLOUDFLARE_GATEWAY_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "kyleboas/research").strip()
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip() or "main"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# ── Config file (config.json) overrides env-var model defaults ────────────────
_cfg_path = ROOT / "config.json"
_CFG: dict = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}
INGEST_POLICY = load_ingest_policy()
RSS_OVERLAP_SECONDS = max(
    0,
    int(os.environ.get("RSS_OVERLAP_SECONDS", str(int(INGEST_POLICY["rss_overlap_seconds"])))),
)
YOUTUBE_OVERLAP_SECONDS = max(
    0,
    int(os.environ.get("YOUTUBE_OVERLAP_SECONDS", str(int(INGEST_POLICY["youtube_overlap_seconds"])))),
)
RSS_FETCH_MAX_WORKERS = max(1, int(os.environ.get("RSS_FETCH_MAX_WORKERS", "2")))
RSS_FEED_MIN_INTERVAL_SECONDS = max(0.0, float(os.environ.get("RSS_FEED_MIN_INTERVAL_SECONDS", "0.75")))
DEFUDDLE_TRANSCRIPT_MIN_INTERVAL_SECONDS = max(0.0, float(os.environ.get("DEFUDDLE_MIN_INTERVAL_SECONDS", "2.0")))
EMBED_MIN_INTERVAL_SECONDS = max(0.0, float(os.environ.get("EMBED_MIN_INTERVAL_SECONDS", "1.0")))
REPORT_POLICY = load_report_policy()
MAX_RESEARCH_ROUNDS = int(REPORT_POLICY["max_research_rounds"])


def set_report_policy(overrides: dict | None = None) -> dict:
    global REPORT_POLICY, MAX_RESEARCH_ROUNDS
    REPORT_POLICY = load_report_policy(overrides)
    MAX_RESEARCH_ROUNDS = int(REPORT_POLICY["max_research_rounds"])
    return REPORT_POLICY


class _RequestPacer:
    def __init__(self, min_interval_seconds: float):
        self.min_interval_seconds = max(0.0, float(min_interval_seconds or 0.0))
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self):
        if self.min_interval_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed_at - now)
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed_at = now + self.min_interval_seconds


_rss_feed_pacer = _RequestPacer(RSS_FEED_MIN_INTERVAL_SECONDS)
_defuddle_transcript_pacer = _RequestPacer(DEFUDDLE_TRANSCRIPT_MIN_INTERVAL_SECONDS)
_embed_pacer = _RequestPacer(EMBED_MIN_INTERVAL_SECONDS)

LEAD_MODEL    = os.environ.get("LEAD_MODEL")    or _CFG.get("lead_model",    "anthropic/claude-sonnet-4-6")
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
        "ingest": common_required,
        "backfill": common_required,
        "detect": common_required,
        "rescore": common_required,
        "report": common_required,
        "all": common_required,
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
        response = _call_with_bad_format_retries(kwargs)
        record_llm_usage(response, model_name=model_name, operation="chat")
        return response
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
REPORT_QUALITY_BAR = (
    "Quality bar:\n"
    "- Write like a top-tier analytical research memo, not a generic blog summary.\n"
    "- Build a defensible thesis, then support it with mechanisms, chronology, comparisons, and counterevidence.\n"
    "- Prefer fewer, sharper claims with dense support over broad but shallow coverage.\n"
    "- Distinguish clearly between direct evidence, inference, and speculation.\n"
    "- When sources disagree, explain the disagreement instead of flattening it away.\n"
    "- Use citations aggressively: every non-obvious factual claim should be traceable to nearby evidence.\n"
    "- Surface limitations honestly, including thin evidence, conflicting evidence, and unanswered questions."
)
REPORT_STRUCTURE_REQUIREMENTS = (
    "Required report structure:\n"
    "# [Descriptive Title]\n"
    "## Executive Summary\n"
    "State the bottom-line conclusion, why it matters, and the strongest supporting evidence.\n"
    "## Key Findings\n"
    "Numbered list of the most consequential findings, each with citations.\n"
    "## Main Analysis\n"
    "Use angle-specific H2 sections with H3 subsections where needed. Synthesize evidence across angles instead of merely restating subagent outputs.\n"
    "## Counterevidence and Alternative Explanations\n"
    "Describe where the evidence is mixed, incomplete, or plausibly explained another way.\n"
    "## Evidence Assessment\n"
    "Assess source quality, source diversity, recency, and any important blind spots.\n"
    "## Implications\n"
    "Explain what the evidence suggests for football tactics going forward.\n"
    "## Open Questions\n"
    "List the most important unresolved questions.\n"
    "## Sources\n"
    "List only sources actually cited in the report, with accurate titles and URLs."
)

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
    "Treat a memo as insufficient if it still lacks direct evidence for the core claim, "
    "counterevidence, important chronology, or the strongest available specifics.\n"
    "If more retrieval is needed, generate a query that is NARROWER and MORE SPECIFIC "
    "than previous queries — do not repeat broad searches."
)

SYS_SUBAGENT = (
    f"You are a focused research subagent. {CITATION_FMT}\n\n"
    "Stay strictly within your assigned boundaries. Do not speculate beyond "
    "what the evidence supports. If evidence is thin, say so explicitly.\n\n"
    f"{REPORT_QUALITY_BAR}\n\n"
    "Your deliverable is an evidence memo for a much larger final report. "
    "Do not write vaguely. Triangulate across sources, identify disagreements, "
    "and separate direct observation from interpretation."
)

SYS_SYNTHESIS = (
    f"You are a synthesis editor merging multiple subagent research outputs into "
    f"one coherent, publication-quality research report. {CITATION_FMT}\n\n"
    "Always write the report in English, regardless of the language of the source material.\n\n"
    f"{REPORT_QUALITY_BAR}\n\n"
    "The final piece should feel like a strong Claude deep-research report: "
    "thesis-driven, richly evidenced, explicit about uncertainty, and willing "
    "to spend words on mechanism and nuance when the evidence supports it."
)

SYS_CITATION = (
    "You are a CitationAgent. Your SOLE job is to verify citations in a research report.\n\n"
    "For EVERY citation [S<source_id>:C<chunk_id>] in the report:\n"
    "1. Verify the source_id and chunk_id exist in the provided chunks\n"
    "2. Verify the cited claim is actually supported by that chunk's content\n"
    "3. Check for claims that SHOULD have citations but don't\n"
    "4. Check for fabricated/hallucinated citation IDs\n"
    "5. Check whether the claim is overstated relative to the cited chunk\n\n"
    "You must also verify the Sources section at the end lists accurate titles and URLs.\n"
    "Treat missing support, overclaiming, and misleading phrasing as citation errors."
)

SYS_REVISION = (
    f"You are a revision editor producing the final research report. {CITATION_FMT}\n\n"
    "You have received a citation verification report from the CitationAgent. "
    "Apply every directive precisely. The final report must have zero citation errors.\n\n"
    "Always write the report in English, regardless of the language of the source material.\n\n"
    f"{REPORT_QUALITY_BAR}\n\n"
    "Preserve analytical depth. Do not shorten the report unless you are removing unsupported or redundant material."
)

SYS_DECOMPOSE = (
    "You are a LeadResearcher orchestrating a multi-agent deep research system. "
    "Your job is to decompose the research topic into non-overlapping subagent tasks "
    "with clear boundaries. Each subagent will run independently with its own context window.\n\n"
    "For every subagent, provide:\n"
    "- a concrete objective\n"
    "- an explicit output format\n"
    "- search guidance describing what evidence or sources to prioritize\n"
    "- boundaries that prevent duplication with other subagents\n\n"
    "Your plan should create enough surface area for a genuinely deep report. "
    "For moderate and complex topics, include dedicated coverage for:\n"
    "- the core mechanism / why the trend is happening\n"
    "- the strongest supporting evidence\n"
    "- counterevidence, competing interpretations, or failure cases\n"
    "- implications and remaining uncertainty\n\n"
    "EFFORT SCALING RULES:\n"
    "- Simple fact-finding: 1 subagent, 3-10 tool/search calls, max_rounds=2\n"
    "- Direct comparisons or moderate analysis: 2-4 subagents, 10-15 calls each, max_rounds=3\n"
    "- Complex multi-faceted research: 5+ subagents with clearly divided responsibilities, max_rounds=5\n\n"
    "You MUST assess which complexity level applies and set parameters accordingly."
)

SYS_SUFFICIENCY = (
    "You are the LeadResearcher evaluating whether the synthesized research is sufficient "
    "or whether additional subagent research rounds are needed.\n\n"
    "Be critical but pragmatic. Only request additional research if there are SPECIFIC, "
    "ACTIONABLE gaps that more retrieval could realistically fill.\n\n"
    "A report is not sufficient if it still lacks support for the core thesis, "
    "fails to address plausible counterarguments, or relies on thin evidence for major claims."
)

# ══════════════════════════════════════════════
# Feed parsing
# ══════════════════════════════════════════════

def parse_rss(path):
    text = path.read_text()
    pairs = []
    seen_urls = set()
    current_name = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue

        match = re.match(r"^(.+?):\s*(https?://\S+)$", line)
        if match and not line.startswith("- "):
            name = match.group(1).strip()
            feed_url = match.group(2).strip()
            if feed_url not in seen_urls:
                pairs.append((name, feed_url))
                seen_urls.add(feed_url)
            current_name = ""
            continue

        name_match = re.match(r"^-\s+\*\*(.+?)\*\*\s*$", line)
        if name_match:
            current_name = name_match.group(1).strip()
            continue

        feed_match = re.match(r"^-\s+Feed:\s*(https?://\S+)\s*$", line)
        if feed_match and current_name:
            feed_url = feed_match.group(1).strip()
            if feed_url not in seen_urls:
                pairs.append((current_name, feed_url))
                seen_urls.add(feed_url)

    return pairs

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
            try:
                channel_id = _resolve_uc_channel_id(channel_source)
            except Exception as e:
                log.warning("YouTube config line could not be resolved: %s (%s)", raw_line, e)
                continue
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


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _compute_overlap_watermark(raw: str | None, overlap_seconds: int) -> datetime | None:
    dt = _parse_iso_datetime(raw)
    if dt is None:
        return None
    return dt - timedelta(seconds=max(0, int(overlap_seconds or 0)))


def _youtube_channel_state_key(channel_id: str) -> str:
    return f"youtube_channel_last_published_at:{channel_id}"


TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref", "source")


def canonicalize_url(url: str) -> str:
    """Return a canonical URL used for ingest dedupe and diagnostics."""
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


def _sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_text_for_hash(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def build_source_dedupe_values(item: dict) -> dict:
    canonical_url = canonicalize_url(item.get("url", ""))
    normalized_content = normalize_text_for_hash(item.get("content", ""))
    return {
        "canonical_url": canonical_url,
        "url_hash": _sha256_text(canonical_url) if canonical_url else "",
        "content_hash": _sha256_text(normalized_content) if normalized_content else "",
    }


def normalize_trend_text(trend: str) -> str:
    return normalize_trend_text_impl(trend)


def trend_fingerprint(trend: str) -> str:
    return trend_fingerprint_impl(trend)

# ══════════════════════════════════════════════
# RSS ingestion
# ══════════════════════════════════════════════

RSS_XML_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
RSS_FEED_USER_AGENT = "ResearchBot/1.0"
RSS_UNDATED_ITEM_LIMIT = 3

def _get(url, headers=None, timeout=15):
    req = Request(url, headers=headers or {"User-Agent": "ResearchBot/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()

def _rss_entry_datetime(entry):
    for path in (
        "pubDate",
        "published",
        "updated",
        "atom:published",
        "atom:updated",
        "dc:date",
    ):
        raw = (entry.findtext(path, default="", namespaces=RSS_XML_NAMESPACES) or "").strip()
        if not raw:
            continue
        try:
            if path == "pubDate":
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
            return _parse_iso_datetime(raw)
        except Exception:
            continue
    return None


def _rss_entry_link(entry):
    link = (entry.findtext("link", default="") or "").strip()
    if link:
        return link
    for node in entry.findall("atom:link", RSS_XML_NAMESPACES):
        href = str(node.attrib.get("href") or "").strip()
        rel = str(node.attrib.get("rel") or "alternate").strip()
        if href and rel in {"", "alternate"}:
            return href
    return ""


def _rss_entry_summary(entry):
    for path in (
        "content:encoded",
        "description",
        "summary",
        "atom:content",
        "atom:summary",
    ):
        raw = entry.findtext(path, default="", namespaces=RSS_XML_NAMESPACES)
        text = strip_html(raw or "")
        if text:
            return text
    return ""


def _rss_entry_author(entry):
    author = (entry.findtext("dc:creator", default="", namespaces=RSS_XML_NAMESPACES) or "").strip()
    if author:
        return author
    author = (entry.findtext("author", default="") or "").strip()
    if author:
        return author
    return (entry.findtext("atom:author/atom:name", default="", namespaces=RSS_XML_NAMESPACES) or "").strip()


def _rss_feed_title(root):
    if root.tag == f"{{{RSS_XML_NAMESPACES['atom']}}}feed":
        return (root.findtext("atom:title", default="", namespaces=RSS_XML_NAMESPACES) or "").strip()
    return (root.findtext("./channel/title", default="") or "").strip()


def _rss_feed_entries(root):
    if root.tag == f"{{{RSS_XML_NAMESPACES['atom']}}}feed":
        return root.findall("atom:entry", RSS_XML_NAMESPACES)
    entries = root.findall("./channel/item")
    return entries if entries else root.findall("item")


def _rss_source_key(feed_url, entry_id, entry_url, title, published_at):
    identity = entry_id or canonicalize_url(entry_url) or f"{title}|{published_at or ''}"
    return f"rss:{_sha256_text(feed_url + '|' + identity)}"


def _fetch_rss_feed_items(feed_name, feed_url, since_dt=None):
    _rss_feed_pacer.wait()
    req = Request(feed_url, headers={"User-Agent": RSS_FEED_USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8"})
    with urlopen(req, timeout=30) as response:
        xml_body = response.read()

    root = ET.fromstring(xml_body)
    feed_title = _rss_feed_title(root) or feed_name
    items = []
    undated_items = 0

    for entry in _rss_feed_entries(root):
        published_at = _rss_entry_datetime(entry)
        if since_dt and published_at and published_at <= since_dt:
            continue
        if since_dt and published_at is None:
            if undated_items >= RSS_UNDATED_ITEM_LIMIT:
                continue
            undated_items += 1

        title = (entry.findtext("title", default="", namespaces=RSS_XML_NAMESPACES) or "").strip()
        if not title:
            title = (entry.findtext("atom:title", default="", namespaces=RSS_XML_NAMESPACES) or "").strip()
        url = _rss_entry_link(entry)
        if not url:
            continue

        rss_content = _rss_entry_summary(entry)
        content = rss_content
        author = _rss_entry_author(entry) or None
        publish_date = published_at.date().isoformat() if published_at else None
        sitename = feed_title or None
        extraction_method = "rss"

        should_attempt_extraction = not rss_content or should_extract(url, rss_content)
        if should_attempt_extraction:
            try:
                article = extract_article(url, fallback_content=rss_content)
                if len(article["content"]) > len(content):
                    content = article["content"]
                    extraction_method = article["extraction_method"]
                    log.info(
                        "Full-text extraction improved %s: %d→%d chars (%s)",
                        title[:40],
                        len(rss_content),
                        len(content),
                        extraction_method,
                    )
                author = article.get("author") or author
                publish_date = article.get("publish_date") or publish_date
                sitename = article.get("sitename") or sitename
                if article.get("title") and not title:
                    title = article["title"]
            except Exception as e:
                log.debug("Full-text extraction failed for %s: %s", url, e)

        if not content:
            continue

        entry_id = (
            (entry.findtext("guid", default="") or "").strip()
            or (entry.findtext("id", default="", namespaces=RSS_XML_NAMESPACES) or "").strip()
            or (entry.findtext("atom:id", default="", namespaces=RSS_XML_NAMESPACES) or "").strip()
        )
        items.append(
            {
                "title": title,
                "url": url,
                "content": content,
                "key": _rss_source_key(feed_url, entry_id, url, title, published_at.isoformat() if published_at else ""),
                "author": author,
                "publish_date": publish_date,
                "sitename": sitename,
                "extraction_method": extraction_method,
                "published_at": published_at.isoformat() if published_at else "",
            }
        )

    return items


def fetch_rss(since_ts=None):
    """Fetch recent stories directly from configured RSS/Atom feeds.

    RSS is treated as discovery: feed entries provide item URLs and basic
    metadata, then article extraction fetches the linked page for full text.
    """
    since_dt = datetime.fromtimestamp(float(since_ts), tz=UTC) if since_ts is not None else None
    feeds = parse_rss(ROOT / "feeds" / "rss.md")
    if not feeds:
        log.warning("No RSS feeds configured in %s", ROOT / "feeds" / "rss.md")
        return []

    items = []
    max_workers = max(1, min(RSS_FETCH_MAX_WORKERS, len(feeds)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_fetch_rss_feed_items, feed_name, feed_url, since_dt): (feed_name, feed_url)
            for feed_name, feed_url in feeds
        }
        for future in as_completed(future_map):
            feed_name, feed_url = future_map[future]
            try:
                items.extend(future.result())
            except Exception as e:
                log.warning("RSS fetch failed for %s (%s): %s", feed_name, feed_url, e)

    items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    return items

# ══════════════════════════════════════════════
# YouTube ingestion
# ══════════════════════════════════════════════

DEFUDDLE_BASE_URL = "https://defuddle.md/"
RETRYABLE_HTTP_STATUSES = {408, 429, 503}
NON_RETRYABLE_HTTP_STATUSES = {400, 401, 402, 403, 404, 422}
YOUTUBE_RSS_BASE_URL = "https://www.youtube.com/feeds/videos.xml"
DEFUDDLE_USER_AGENT = "ResearchBot/1.0"
YOUTUBE_RSS_USER_AGENT = "ResearchBot/1.0"


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


def _http_get_text(url, *, headers=None, label="HTTP request"):
    if label.startswith("defuddle transcript fetch"):
        _defuddle_transcript_pacer.wait()
    request_headers = {"User-Agent": DEFUDDLE_USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = Request(url, headers=request_headers)
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(req, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            status = int(e.code)
            body, response_headers = _http_error_details(e)
            if status in RETRYABLE_HTTP_STATUSES and attempt < max_attempts:
                delay = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.2)
                log.warning(
                    "%s retryable failure status=%s attempt=%s/%s url=%s; retrying in %.2fs",
                    label,
                    status,
                    attempt,
                    max_attempts,
                    url,
                    delay,
                )
                time.sleep(delay)
                continue

            level = log.warning if status in NON_RETRYABLE_HTTP_STATUSES else log.error
            level(
                "%s failed status=%s url=%s headers=%s body=%s",
                label,
                status,
                url,
                response_headers,
                body[:3000],
            )
            raise


def _youtube_rss_latest_videos(channel_id, limit=None):
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
        published_at = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
        link = ""
        link_node = entry.find("atom:link", ns)
        if link_node is not None:
            link = str(link_node.attrib.get("href") or "").strip()
        videos.append({"id": video_id, "title": title, "url": link, "published_at": published_at})
        if limit is not None and len(videos) >= limit:
            break
    return videos


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


def _clean_markdown_transcript(text):
    cleaned = str(text or "")
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"^\*\*\d{1,2}:\d{2}(?::\d{2})?\*\*\s*[·-]?\s*", "", cleaned, flags=re.M)
    cleaned = cleaned.replace("\\[", "[").replace("\\]", "]")
    cleaned = re.sub(r"\[(.*?)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_youtube_transcript_from_markdown(markdown):
    metadata, body = _parse_markdown_frontmatter(markdown)
    match = re.search(r"^##\s+Transcript\s*$", body, re.M)
    transcript_body = body[match.end() :] if match else body
    next_heading = re.search(r"^##\s+", transcript_body, re.M)
    if next_heading:
        transcript_body = transcript_body[: next_heading.start()]
    return {
        "title": str(metadata.get("title") or "").strip(),
        "transcript": _clean_markdown_transcript(transcript_body),
    }


def _defuddle_markdown_url(source_url):
    cleaned = str(source_url or "").strip()
    return f"{DEFUDDLE_BASE_URL}{quote(cleaned, safe=':/?&=#')}"


def _fetch_youtube_transcript(video_id):
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    markdown = _http_get_text(
        _defuddle_markdown_url(video_url),
        headers={"Accept": "text/markdown", "User-Agent": DEFUDDLE_USER_AGENT},
        label="defuddle transcript fetch",
    )
    return _extract_youtube_transcript_from_markdown(markdown)


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
    rewritten_lines = []
    changed = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            rewritten_lines.append(raw_line)
            continue
        match = re.match(r"^(.+?):\s*(https?://\S+)$", line)
        if not match:
            rewritten_lines.append(raw_line)
            continue
        name = match.group(1).strip()
        source = match.group(2).strip()
        try:
            canonical_id = _resolve_uc_channel_id(source)
        except Exception as e:
            log.warning("YouTube source normalization failed for source=%s: %s", source, e)
            rewritten_lines.append(raw_line)
            continue
        canonical_line = f"{name}: https://www.youtube.com/channel/{canonical_id}"
        rewritten_lines.append(canonical_line)
        if canonical_line != line:
            changed = True

    if not any(line.strip() for line in rewritten_lines):
        # Backward compatibility: parse old list format and rewrite.
        names = [m.group(1) for m in re.finditer(r"^-\s+\*\*(.+?)\*\*", text, re.M)]
        sources = [m.group(1) for m in re.finditer(r"^\s+-\s+(?:Canonical\s+)?Channel ID:\s*(\S+)", text, re.M)]
        rewritten_lines = []
        for name, source in zip(names, sources):
            try:
                canonical_id = _resolve_uc_channel_id(source)
            except Exception as e:
                log.warning("YouTube source normalization failed for source=%s: %s", source, e)
                continue
            rewritten_lines.append(f"{name}: https://www.youtube.com/channel/{canonical_id}")
        changed = bool(rewritten_lines)

    if changed and rewritten_lines:
        path.write_text("\n".join(rewritten_lines).rstrip() + "\n")
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


def fetch_youtube(name, channel_id, published_after=None):
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
        return [], True, counters, None
    try:
        videos = _youtube_rss_latest_videos(resolved_channel_id)
        counters["youtube_discovery_successes"] += 1
    except HTTPError as e:
        if int(e.code) in RETRYABLE_HTTP_STATUSES:
            counters["youtube_discovery_retryable_failures"] += 1
        log.warning(
            "YouTube RSS discovery failed for %s (%s) status=%s",
            name,
            resolved_channel_id,
            e.code,
        )
        return [], True, counters, None
    except Exception as e:
        counters["youtube_discovery_retryable_failures"] += 1
        log.warning(
            "YouTube discovery failed for %s (%s): %s",
            name,
            resolved_channel_id,
            e,
        )
        return [], True, counters, None

    if not videos:
        log.info(
            "YouTube discovery returned no videos for %s (%s).",
            name,
            resolved_channel_id,
        )

    items = []
    latest_published_at = None
    for video in videos:
        video_published_at = _parse_iso_datetime(video.get("published_at"))
        if video_published_at and (latest_published_at is None or video_published_at > latest_published_at):
            latest_published_at = video_published_at
        if published_after and video_published_at and video_published_at <= published_after:
            continue
        vid = _video_id(video)
        if not vid:
            continue
        title = _video_title(video)
        try:
            transcript_data = _fetch_youtube_transcript(vid)
            transcript = str(transcript_data.get("transcript") or "").strip()
        except HTTPError as e:
            log.warning("Transcript %s failed for channel=%s title=%r status=%s", vid, name, title, e.code)
            counters["youtube_transcript_failures"] += 1
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
                    "published_at": video.get("published_at"),
                }
            )
    return items, False, counters, latest_published_at

# ══════════════════════════════════════════════
# Storage & embedding
# ══════════════════════════════════════════════

def source_exists_by_key(conn, source_key):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM sources WHERE source_key = %s", (source_key,))
        return cur.fetchone() is not None

def find_existing_source(conn, source_key, url_hash="", content_hash=""):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sources WHERE source_key = %s LIMIT 1", (source_key,))
        row = cur.fetchone()
        if row:
            return row[0], "source_key"

        if url_hash:
            cur.execute("SELECT id FROM sources WHERE url_hash = %s LIMIT 1", (url_hash,))
            row = cur.fetchone()
            if row:
                return row[0], "url_hash"

        if content_hash:
            cur.execute("SELECT id FROM sources WHERE content_hash = %s LIMIT 1", (content_hash,))
            row = cur.fetchone()
            if row:
                return row[0], "content_hash"

    return None, None

def store_source(conn, item, source_type):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (source_type, source_key, title, url, content, "
            "author, publish_date, sitename, extraction_method, canonical_url, url_hash, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (source_key) DO NOTHING RETURNING id",
            (source_type, item["key"], item["title"], item["url"], item["content"],
             item.get("author"), item.get("publish_date"), item.get("sitename"),
             item.get("extraction_method", "rss"), item.get("canonical_url"),
             item.get("url_hash"), item.get("content_hash")),
        )
        row = cur.fetchone()
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
            _embed_pacer.wait()
            resp = client.embeddings.create(model=_resolved_embed_model, input=cleaned_inputs)
            record_llm_usage(resp, model_name=_resolved_embed_model, operation="embedding")
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
        conn.commit()
        return

    chunk_texts = [c["content"] for c in chunk_records]
    vectors = embed(chunk_texts)
    if not vectors:
        log.warning("Skipping chunk insert for source_id=%s because embeddings were unavailable", source_id)
        set_source_embed_status(conn, source_id, "embed_failed", "Embeddings unavailable (request failed or rejected)")
        conn.commit()
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
    except Exception as exc:
        conn.rollback()
        log.exception("Chunk embed/store failed for source_id=%s: %s", source_id, exc)
        set_source_embed_status(conn, source_id, "embed_failed", str(exc))
        conn.commit()
        return

    set_source_embed_status(conn, source_id, "embedded")
    conn.commit()

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


def upsert_trend_candidate(conn, candidate: dict, feedback_adjustment: int):
    return upsert_trend_candidate_impl(conn, candidate, feedback_adjustment)


def _effective_source_diversity(stored_source_diversity: int | None, linked_source_count: int | None) -> int:
    return effective_source_diversity_impl(stored_source_diversity, linked_source_count)


def _rescored_trend_candidate_values(
    *,
    base_score: int,
    feedback_adjustment: int,
    stored_source_diversity: int | None,
    linked_source_count: int | None,
    novelty_score: float | None,
) -> tuple[int, int]:
    return rescored_trend_candidate_values_impl(
        base_score=base_score,
        feedback_adjustment=feedback_adjustment,
        stored_source_diversity=stored_source_diversity,
        linked_source_count=linked_source_count,
        novelty_score=novelty_score,
    )


def _parse_rescore_statuses(raw: str | None) -> list[str] | None:
    return parse_rescore_statuses_impl(raw)

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

def chunk_rows_to_records(rows):
    records = []
    for row in rows:
        if isinstance(row, dict):
            records.append(
                {
                    "chunk_id": row.get("chunk_id"),
                    "source_id": row.get("source_id"),
                    "content": row.get("content", ""),
                    "source_title": row.get("source_title", ""),
                    "source_url": row.get("source_url", ""),
                    **({"score": row.get("score")} if row.get("score") is not None else {}),
                }
            )
            continue

        cid, sid, content, title, url, *rest = row
        record = {
            "chunk_id": cid,
            "source_id": sid,
            "content": content,
            "source_title": title,
            "source_url": url,
        }
        if rest:
            record["score"] = rest[0]
        records.append(record)
    return records


def chunk_records_to_context(records):
    """Format retrieved chunk records as a JSON context packet."""
    return json.dumps(
        [
            {
                "chunk_id": record.get("chunk_id"),
                "source_id": record.get("source_id"),
                "content": record.get("content", ""),
                "source_title": record.get("source_title", ""),
                "source_url": record.get("source_url", ""),
            }
            for record in records
        ],
        indent=2,
        ensure_ascii=False,
    )


def chunks_to_context(rows):
    """Format retrieved chunk rows as a JSON context packet."""
    return chunk_records_to_context(chunk_rows_to_records(rows))

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


REPORT_RUNS_DIR = ROOT / "report_runs"
MAX_SUBAGENT_ROUNDS = 5


def _slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or fallback


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def _write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _strip_markdown_to_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"^---\s*[\s\S]*?\n---\s*", "", text, flags=re.M)
    text = re.sub(r"<!---?more--->", " ", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[S\d+:C\d+\]", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^[#>\-\*\d\.\s]+", "", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate_chars(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    shortened = normalized[: max(0, limit - 1)].rstrip()
    return f"{shortened}…"


def _report_summary(report_body: str, *, limit: int = 255) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", str(report_body or "")) if part.strip()]
    candidates = []
    for part in paragraphs:
        if part.lstrip().startswith("#"):
            continue
        plain = _strip_markdown_to_text(part)
        if plain:
            candidates.append(plain)
    summary_source = candidates[0] if candidates else _strip_markdown_to_text(report_body)
    return _truncate_chars(summary_source or "No summary available.", limit)


def _report_category(title: str, report_body: str) -> str:
    haystack = f"{title}\n{report_body}".lower()
    category_map = [
        ("Premier League", ["premier league"]),
        ("Champions League", ["champions league"]),
        ("Europa League", ["europa league"]),
        ("Conference League", ["conference league"]),
        ("La Liga", ["la liga"]),
        ("Serie A", ["serie a"]),
        ("Bundesliga", ["bundesliga"]),
        ("Ligue 1", ["ligue 1"]),
        ("MLS", ["major league soccer", "mls"]),
        ("NWSL", ["nwsl"]),
        ("Women's Super League", ["women's super league", "wsl"]),
        ("FA Cup", ["fa cup"]),
        ("Championship", ["efl championship", "championship"]),
    ]
    for label, needles in category_map:
        if any(needle in haystack for needle in needles):
            return label
    return "General"


def _report_post_relative_path(title: str, created_at: datetime) -> str:
    stamp = created_at.astimezone(UTC)
    slug = _slugify(title, "report")[:80]
    return f"_posts/{stamp.strftime('%Y')}/{stamp.strftime('%m')}/{stamp.strftime('%Y-%m-%d')}-{slug}.md"


def _report_post_content(title: str, report_body: str, *, created_at: datetime, category: str) -> str:
    stamp = created_at.astimezone(UTC)
    summary = _report_summary(report_body)
    body = str(report_body or "").strip()
    front_matter = [
        "---",
        "layout: post",
        f"date: {stamp.strftime('%Y-%m-%d %H:%M UTC')}",
        f"title: {json.dumps(title or 'Untitled report', ensure_ascii=False)}",
        "categories:",
        f"- {json.dumps(category or 'General', ensure_ascii=False)}",
        "---",
        "",
        summary,
        "",
        "<!---more--->",
        "",
        body,
        "",
    ]
    return "\n".join(front_matter)


def _github_blob_url(path: str, *, repo: str | None = None, branch: str | None = None) -> str:
    resolved_repo = (repo or GITHUB_REPO or "").strip()
    resolved_branch = (branch or GITHUB_BRANCH or "main").strip() or "main"
    if not resolved_repo:
        return ""
    return f"https://github.com/{resolved_repo}/blob/{resolved_branch}/{path}"


def _github_request(url: str, *, method: str = "GET", payload: dict | None = None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "research-bot",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _github_existing_file_sha(path: str, *, repo: str | None = None, branch: str | None = None) -> str | None:
    resolved_repo = (repo or GITHUB_REPO or "").strip()
    resolved_branch = (branch or GITHUB_BRANCH or "main").strip() or "main"
    if not resolved_repo:
        return None
    url = f"https://api.github.com/repos/{resolved_repo}/contents/{quote(path, safe='/')}?ref={quote(resolved_branch)}"
    try:
        payload = _github_request(url)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return str(payload.get("sha") or "").strip() or None


def _github_branch_head_sha(branch: str, *, repo: str | None = None) -> str:
    resolved_repo = (repo or GITHUB_REPO or "").strip()
    resolved_branch = (branch or GITHUB_BRANCH or "main").strip() or "main"
    if not resolved_repo:
        raise RuntimeError("missing_github_repo")
    url = f"https://api.github.com/repos/{resolved_repo}/git/ref/heads/{quote(resolved_branch, safe='')}"
    payload = _github_request(url)
    obj = payload.get("object") or {}
    sha = str(obj.get("sha") or "").strip()
    if not sha:
        raise RuntimeError(f"missing_branch_sha:{resolved_branch}")
    return sha


def _github_create_branch(branch: str, *, repo: str | None = None, from_branch: str | None = None) -> str:
    resolved_repo = (repo or GITHUB_REPO or "").strip()
    head_branch = str(branch or "").strip()
    base_branch = (from_branch or GITHUB_BRANCH or "main").strip() or "main"
    if not resolved_repo:
        raise RuntimeError("missing_github_repo")
    if not head_branch:
        raise RuntimeError("missing_github_branch")
    sha = _github_branch_head_sha(base_branch, repo=resolved_repo)
    url = f"https://api.github.com/repos/{resolved_repo}/git/refs"
    try:
        _github_request(
            url,
            method="POST",
            payload={"ref": f"refs/heads/{head_branch}", "sha": sha},
        )
    except HTTPError as exc:
        if exc.code != 422:
            raise
    return head_branch


def _report_post_branch_name(title: str, created_at: datetime) -> str:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    slug = _slugify(title, "report")[:60]
    return f"report-post/{stamp}-{slug}"


def _report_post_pr_title(title: str) -> str:
    return f"Add report: {str(title or 'Untitled report').strip()}"


def _report_post_pr_body(path: str, summary: str, *, category: str, created_at: datetime) -> str:
    stamp = created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join(
        [
            "Automated report post generated by the research pipeline.",
            "",
            f"- Path: `{path}`",
            f"- Date: {stamp}",
            f"- Category: {category or 'General'}",
            "",
            summary or "No summary available.",
        ]
    )


def _github_create_pull_request(
    *,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str,
    repo: str | None = None,
) -> str:
    resolved_repo = (repo or GITHUB_REPO or "").strip()
    if not GITHUB_TOKEN:
        raise RuntimeError("missing_github_token")
    if not resolved_repo:
        raise RuntimeError("missing_github_repo")
    url = f"https://api.github.com/repos/{resolved_repo}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }
    result = _github_request(url, method="POST", payload=payload)
    html_url = str(result.get("html_url") or "").strip()
    if not html_url:
        raise RuntimeError("missing_pull_request_url")
    return html_url


def _discord_notify_report_pr(pr_url: str, *, title: str, path: str, branch: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        return
    content = (
        f"New report PR created: {title}\n"
        f"PR: {pr_url}\n"
        f"Branch: {branch}\n"
        f"Path: {path}"
    )
    payload = json.dumps({"content": content}).encode("utf-8")
    req = Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "research-bot",
        },
        method="POST",
    )
    with urlopen(req, timeout=30) as response:
        response.read()


def _publish_report_post_to_github(
    path: str,
    content: str,
    *,
    title: str,
    summary: str,
    category: str,
    created_at: datetime,
    repo: str | None = None,
    branch: str | None = None,
) -> dict:
    resolved_repo = (repo or GITHUB_REPO or "").strip()
    base_branch = (branch or GITHUB_BRANCH or "main").strip() or "main"
    if not GITHUB_TOKEN:
        raise RuntimeError("missing_github_token")
    if not resolved_repo:
        raise RuntimeError("missing_github_repo")
    head_branch = _github_create_branch(
        _report_post_branch_name(title, created_at),
        repo=resolved_repo,
        from_branch=base_branch,
    )
    sha = _github_existing_file_sha(path, repo=resolved_repo, branch=head_branch)
    payload = {
        "message": f"Save report post: {Path(path).name}",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": head_branch,
    }
    if sha:
        payload["sha"] = sha

    url = f"https://api.github.com/repos/{resolved_repo}/contents/{quote(path, safe='/')}"
    result = _github_request(url, method="PUT", payload=payload)
    content_payload = result.get("content") or {}
    html_url = str(content_payload.get("html_url") or "").strip()
    pr_url = _github_create_pull_request(
        title=_report_post_pr_title(title),
        body=_report_post_pr_body(path, summary, category=category, created_at=created_at),
        head_branch=head_branch,
        base_branch=base_branch,
        repo=resolved_repo,
    )
    discord_notify_error = ""
    try:
        _discord_notify_report_pr(pr_url, title=title, path=path, branch=head_branch)
    except Exception as exc:
        discord_notify_error = str(exc)
        log.error("Discord report PR notify failed for %s: %s", pr_url, exc, exc_info=True)
    return {
        "base_branch": base_branch,
        "branch": head_branch,
        "discord_notify_error": discord_notify_error or None,
        "pr_url": pr_url,
        "content_url": html_url or _github_blob_url(path, repo=resolved_repo, branch=head_branch),
    }


def _report_run_dir_for_trend(trend: str) -> Path:
    REPORT_RUNS_DIR.mkdir(exist_ok=True)
    base_name = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{_slugify(trend, 'trend')[:60]}"
    run_dir = REPORT_RUNS_DIR / base_name
    suffix = 2
    while run_dir.exists():
        run_dir = REPORT_RUNS_DIR / f"{base_name}-{suffix:02d}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _round_dir(run_dir: Path, research_round: int) -> Path:
    return run_dir / f"round-{research_round:02d}"


def _subagent_artifact_dir(run_dir: Path, research_round: int, task_order: int, angle: str) -> Path:
    return _round_dir(run_dir, research_round) / "subagents" / f"{task_order:02d}-{_slugify(angle, 'angle')[:50]}"


def _coerce_positive_int(value, default: int, *, minimum: int = 1, maximum: int = MAX_SUBAGENT_ROUNDS) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = default
    return max(minimum, min(maximum, coerced))


def _normalize_text_field(value, default: str) -> str:
    if value is None:
        text = default
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    normalized = text.strip()
    return normalized or default


def _normalize_subagent_task(task, index: int, trend: str, complexity: str = "moderate"):
    raw = task if isinstance(task, dict) else {}
    angle = _normalize_text_field(raw.get("angle"), f"angle-{index}")
    objective = _normalize_text_field(
        raw.get("objective"),
        f"Find the strongest evidence for {trend} from the angle '{angle}'.",
    )
    queries = [str(q).strip() for q in raw.get("search_queries", []) if str(q).strip()]
    if not queries:
        queries = [f"{trend} {angle}"]
    normalized_complexity = (complexity or "moderate").lower()
    default_rounds = {
        "simple": int(REPORT_POLICY["simple_default_rounds"]),
        "moderate": int(REPORT_POLICY["moderate_default_rounds"]),
        "complex": int(REPORT_POLICY["complex_default_rounds"]),
    }.get(normalized_complexity, int(REPORT_POLICY["moderate_default_rounds"]))
    return {
        "task_order": index,
        "angle": angle,
        "objective": objective,
        "search_queries": queries,
        "boundaries": _normalize_text_field(
            raw.get("boundaries"),
            "Avoid duplicating other angles and avoid unsupported speculation.",
        ),
        "output_format": _normalize_text_field(
            raw.get("output_format"),
            "Return a markdown brief with a direct answer, strongest evidence, mechanism, counterevidence, limitations, and unresolved questions.",
        ),
        "search_guidance": _normalize_text_field(
            raw.get("search_guidance"),
            "Start broad, then narrow toward recent, contradictory, especially concrete, or especially explanatory evidence.",
        ),
        "max_rounds": _coerce_positive_int(raw.get("max_rounds"), default_rounds),
    }


def _pad_subagent_tasks(tasks: list[dict], trend: str, complexity: str) -> list[dict]:
    normalized_complexity = (complexity or "moderate").lower()
    min_tasks = {
        "simple": 1,
        "moderate": int(REPORT_POLICY["moderate_min_tasks"]),
        "complex": int(REPORT_POLICY["complex_min_tasks"]),
    }.get(normalized_complexity, int(REPORT_POLICY["moderate_min_tasks"]))
    if len(tasks) >= min_tasks:
        return tasks

    existing_angles = {str(task.get("angle", "")).strip().lower() for task in tasks}
    fallback_specs = [
        {
            "angle": "Core evidence and mechanism",
            "objective": f"Establish the strongest direct evidence for {trend} and explain the main tactical mechanism behind it.",
            "search_queries": [trend, f"{trend} tactical mechanism", f"{trend} evidence examples"],
            "boundaries": "Focus on proving and explaining the trend itself; do not spend much time on future implications.",
            "output_format": "Return a memo centered on the core evidence, mechanism, and the most concrete examples.",
            "search_guidance": "Prioritize concrete tactical descriptions, repeated match patterns, and source material that explains why the pattern works.",
        },
        {
            "angle": "Counterevidence and failure cases",
            "objective": f"Find the strongest evidence against {trend}, including cases where it failed, was overstated, or is better explained another way.",
            "search_queries": [f"{trend} counterevidence", f"{trend} limitations", f"{trend} failure cases"],
            "boundaries": "Focus on disagreement and limitations rather than re-arguing the main positive case.",
            "output_format": "Return a memo that stresses disagreement, edge cases, and what would weaken the main thesis.",
            "search_guidance": "Prioritize contradictory, skeptical, or qualification-heavy evidence.",
        },
        {
            "angle": "Implications and tactical consequences",
            "objective": f"Explain what {trend} changes in practice, who benefits, what adaptations it invites, and where it may go next.",
            "search_queries": [f"{trend} implications", f"{trend} adaptations", f"{trend} future outlook"],
            "boundaries": "Focus on practical implications and forward-looking tactical consequences, not on re-establishing the base evidence.",
            "output_format": "Return a memo covering consequences, adaptations, and clearly marked uncertainty about what comes next.",
            "search_guidance": "Prioritize analysis that connects evidence to practical coaching or match implications.",
        },
        {
            "angle": "Concrete examples and comparison points",
            "objective": f"Collect the clearest team, match, or player examples that illustrate {trend} and compare how it appears across contexts.",
            "search_queries": [f"{trend} examples", f"{trend} team analysis", f"{trend} comparison"],
            "boundaries": "Focus on concrete examples and comparisons rather than abstract theory.",
            "output_format": "Return a memo built around examples, comparisons, and what those comparisons reveal.",
            "search_guidance": "Prioritize evidence-rich examples with enough detail to compare contexts directly.",
        },
        {
            "angle": "Historical context and adoption",
            "objective": f"Place {trend} in context by showing how recent it is, what preceded it, and whether it looks early, growing, or already mainstream.",
            "search_queries": [f"{trend} historical context", f"{trend} evolution", f"{trend} adoption"],
            "boundaries": "Focus on timeline, context, and adoption rather than detailed tactical mechanics.",
            "output_format": "Return a memo covering the timeline, precursors, and current adoption level of the trend.",
            "search_guidance": "Prioritize evidence that helps establish chronology, diffusion, and whether the trend is actually new.",
        },
    ]

    next_index = len(tasks) + 1
    for spec in fallback_specs:
        if len(tasks) >= min_tasks:
            break
        angle_key = spec["angle"].strip().lower()
        if angle_key in existing_angles:
            continue
        tasks.append(_normalize_subagent_task(spec, next_index, trend, normalized_complexity))
        existing_angles.add(angle_key)
        next_index += 1
    return tasks


def _persist_lead_plan(conn, run_dir: Path, trend: str, plan: dict):
    payload = {
        "trend": trend,
        "saved_at": datetime.now(UTC).isoformat(),
        **plan,
    }
    _write_json(run_dir / "lead-plan.json", payload)
    markdown = [
        "# Lead Research Plan",
        "",
        f"- Topic: {trend}",
        f"- Complexity: {plan.get('complexity', 'unknown')}",
        f"- Reasoning: {plan.get('reasoning') or 'Not provided'}",
        "",
    ]
    for task in plan.get("tasks", []):
        markdown.extend(
            [
                f"## {task['task_order']}. {task['angle']}",
                "",
                f"**Objective:** {task['objective']}",
                "",
                f"**Task boundaries:** {task['boundaries']}",
                "",
                f"**Output format:** {task['output_format']}",
                "",
                f"**Search guidance:** {task['search_guidance']}",
                "",
                "**Search queries:**",
                *(f"- {query}" for query in task["search_queries"]),
                "",
            ]
        )
    _write_text(run_dir / "lead-plan.md", "\n".join(markdown).strip() + "\n")
    save_state(
        conn,
        "last_report_plan",
        json.dumps(
            {
                "trend": trend,
                "run_dir": str(run_dir),
                "complexity": plan.get("complexity"),
                "saved_at": payload["saved_at"],
            },
            ensure_ascii=False,
        ),
    )

# ══════════════════════════════════════════════
# Trend detection
# ══════════════════════════════════════════════

def _tokenize_feedback_text(text: str) -> list[str]:
    return tokenize_feedback_text_impl(text)


def _load_feedback_keyword_weights(conn) -> dict[str, float]:
    return load_feedback_keyword_weights_impl(conn)


def _load_feedback_embeddings(conn) -> list[tuple[list[float], int]]:
    return load_feedback_embeddings_impl(conn, embed_fn=embed)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    from detect_scoring import cosine_similarity

    return cosine_similarity(a, b)


def _feedback_adjustment_for_trend(
    trend: str,
    keyword_weights: dict[str, float],
    feedback_embeddings: list[tuple[list[float], int]] | None = None,
) -> int:
    return feedback_adjustment_for_trend_impl(
        trend,
        keyword_weights,
        feedback_embeddings,
        embed_fn=embed,
    )


def _detect_novel_tactical_patterns(conn, past_topics):
    return detect_novel_tactical_patterns_impl(conn, past_topics, embed_fn=embed)


def detect_trends(conn) -> tuple[list[dict], bool]:
    return detect_trends_impl(
        conn,
        config_path=ROOT / "config.json",
        run_bertrend_detection_fn=run_bertrend_detection,
        describe_signals_with_llm_fn=describe_signals_with_llm,
        ask_fn=ask,
        signal_model=SIGNAL_MODEL,
        embed_fn=embed,
        parse_json_fn=parse_json,
    )


def _detect_trends_llm_only(conn, past) -> tuple[list[dict], bool]:
    return detect_trends_llm_only_impl(conn, past, ask_fn=ask, parse_json_fn=parse_json)

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
        "4. What boundaries prevent duplication between angles?\n"
        "5. Which angle will handle counterevidence or alternative explanations?\n"
        "6. Which angle will handle implications and unresolved uncertainty?\n\n"
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
        '      "output_format": "exact shape the subagent should return",\n'
        '      "search_guidance": "what evidence types or source behavior to prioritize",\n'
        '      "max_rounds": 3\n'
        '    }\n'
        '  ]\n'
        '}\n'
        "```",
        budget_tokens=10000,
    )
    log.info("Lead agent thinking: %s...", thinking[:200] if thinking else "(none)")

    data = parse_json(response)
    if isinstance(data, list):
        data = {"tasks": data}
    elif not isinstance(data, dict):
        data = {"tasks": [data]}

    complexity = str(data.get("complexity", "moderate") or "moderate").lower()
    raw_tasks = data.get("tasks", [])
    if isinstance(raw_tasks, dict):
        raw_tasks = raw_tasks.get("tasks") or [raw_tasks]
    if not isinstance(raw_tasks, list):
        raw_tasks = [raw_tasks]
    tasks = [_normalize_subagent_task(task, index + 1, trend, complexity) for index, task in enumerate(raw_tasks)]
    if not tasks:
        tasks = [_normalize_subagent_task({}, 1, trend, complexity)]
    tasks = _pad_subagent_tasks(tasks, trend, complexity)
    log.info("Lead agent: complexity=%s, %d angles: %s",
             complexity, len(tasks), [t.get("angle") for t in tasks])
    return {
        "complexity": complexity,
        "reasoning": data.get("reasoning", ""),
        "tasks": tasks,
    }

# ══════════════════════════════════════════════
# Step 2: Subagent — OODA retrieval loop (broad-to-narrow)
# ══════════════════════════════════════════════

def research_angle(conninfo, trend, task, run_dir: Path, research_round: int):
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
    output_format = task.get("output_format", "")
    search_guidance = task.get("search_guidance", "")
    max_rounds = task.get("max_rounds", 3)
    task_order = int(task.get("task_order", 0) or 0)
    artifact_dir = _subagent_artifact_dir(run_dir, research_round, task_order, angle)
    _write_json(artifact_dir / "task.json", task)
    all_chunks = {}  # chunk_id -> record, deduplicated

    with psycopg.connect(conninfo) as conn:
        for round_num in range(max_rounds):
            query = queries[round_num] if round_num < len(queries) else queries[-1]
            log.info("  Subagent '%s' round %d/%d: query='%s'", angle, round_num + 1, max_rounds, query[:60])
            rows = hybrid_search(conn, query, limit=int(REPORT_POLICY["subagent_search_limit"]))
            for record in chunk_rows_to_records(rows):
                all_chunks[record["chunk_id"]] = record

            if not all_chunks:
                continue

            chunk_json = chunk_records_to_context(list(all_chunks.values()))
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

            next_q = eval_result.get("next_query")
            if next_q:
                queries.append(next_q)

    last_coverage = eval_result.get("coverage_pct", 50) if 'eval_result' in dir() else 50

    if not all_chunks:
        summary = f"No evidence found for: {angle}"
        _write_json(artifact_dir / "evidence.json", [])
        _write_text(artifact_dir / "summary.md", summary)
        result = {
            "task_order": task_order,
            "angle": angle,
            "coverage": 0,
            "chunk_count": 0,
            "artifact_dir": str(artifact_dir),
            "summary_path": str(artifact_dir / "summary.md"),
            "evidence_path": str(artifact_dir / "evidence.json"),
        }
        _write_json(artifact_dir / "result.json", result)
        return result

    chunk_records = list(all_chunks.values())
    chunk_json = chunk_records_to_context(chunk_records)

    # Write grounded summary for this angle
    summary = ask(
        SYS_SUBAGENT,

        f"Angle: {angle}\n"
        f"Objective: {objective}\n"
        f"Out of scope: {boundaries}\n\n"
        f"Search guidance: {search_guidance}\n"
        f"Required output format: {output_format}\n\n"
        f"Evidence chunks:\n{chunk_json}\n\n"
        "Write a thorough, evidence-grounded memo for this angle.\n\n"
        "Required structure:\n"
        "## Bottom Line\n"
        "State the strongest supported conclusion for this angle.\n"
        "## Evidence\n"
        "Lay out the most important facts, comparisons, chronology, and mechanisms.\n"
        "## Counterevidence / Alternative Interpretations\n"
        "Explain disagreement, edge cases, or plausible alternative readings of the evidence.\n"
        "## Confidence and Limitations\n"
        "Assess how strong the evidence is and what remains thin.\n"
        "## Unresolved Questions\n"
        "List what further retrieval would still need to answer.\n\n"
        "Requirements:\n"
        "- Lead with the strongest finding, not background filler\n"
        "- Use inline citations [S<source_id>:C<chunk_id>] on every non-obvious claim\n"
        "- Prefer claims supported by multiple chunks when possible\n"
        "- Bold key statistics and figures\n"
        "- Be explicit when a sentence is inference rather than direct evidence\n"
        "- Flag if evidence was insufficient for any part of the objective",
        model=SUMMARY_MODEL,
        max_tokens=int(REPORT_POLICY["subagent_max_tokens"]),
    )
    _write_json(artifact_dir / "evidence.json", chunk_records)
    _write_text(artifact_dir / "summary.md", summary)
    log.info("Subagent '%s' done: %d chunks, %d rounds", angle, len(all_chunks), round_num + 1)
    result = {
        "task_order": task_order,
        "angle": angle,
        "coverage": last_coverage,
        "chunk_count": len(chunk_records),
        "artifact_dir": str(artifact_dir),
        "summary_path": str(artifact_dir / "summary.md"),
        "evidence_path": str(artifact_dir / "evidence.json"),
    }
    _write_json(artifact_dir / "result.json", result)
    return result

def run_subagents(trend, tasks, run_dir: Path, research_round: int):
    """Run subagent research in parallel with bounded concurrency."""
    conninfo, reason = resolve_database_conninfo()
    if not conninfo:
        raise RuntimeError(f"database_unavailable:{reason}")
    results = []
    if not tasks:
        return results
    with ThreadPoolExecutor(max_workers=max(1, min(len(tasks), 4))) as pool:
        futures = {
            pool.submit(research_angle, conninfo, trend, task, run_dir, research_round): task
            for task in tasks
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                task = futures[future]
                log.warning("Subagent '%s' failed: %s", task.get("angle"), e)
                artifact_dir = _subagent_artifact_dir(
                    run_dir,
                    research_round,
                    int(task.get("task_order", 0) or 0),
                    task.get("angle", "?"),
                )
                failure_summary = f"Research failed: {e}"
                _write_json(artifact_dir / "evidence.json", [])
                _write_text(artifact_dir / "summary.md", failure_summary)
                result = {
                    "task_order": int(task.get("task_order", 0) or 0),
                    "angle": task.get("angle", "?"),
                    "coverage": 0,
                    "chunk_count": 0,
                    "artifact_dir": str(artifact_dir),
                    "summary_path": str(artifact_dir / "summary.md"),
                    "evidence_path": str(artifact_dir / "evidence.json"),
                }
                _write_json(artifact_dir / "result.json", result)
                results.append(result)
    return results

# ══════════════════════════════════════════════
# Step 3: Synthesis — merge subagent outputs
# ══════════════════════════════════════════════

def collect_all_chunks(subagent_results):
    """Deduplicate chunks across all subagent results."""
    all_chunks = {}
    for r in subagent_results:
        evidence_path = r.get("evidence_path")
        records = []
        if evidence_path and Path(evidence_path).exists():
            records = json.loads(Path(evidence_path).read_text())
        else:
            records = chunk_rows_to_records(r.get("chunks", []))
        for record in records:
            all_chunks[record["chunk_id"]] = record
    return list(all_chunks.values())

def synthesize(trend, subagent_results, run_dir: Path, research_round: int):
    """Merge parallel subagent summaries into a cohesive draft report."""
    ordered_results = sorted(subagent_results, key=lambda result: result.get("task_order", 0))
    summaries_text = "\n\n---\n\n".join(
        f"### Angle: {r['angle']} (coverage: {r.get('coverage', '?')}%)\n\n{Path(r['summary_path']).read_text()}"
        for r in ordered_results
    )
    all_chunks = collect_all_chunks(subagent_results)
    chunk_json = chunk_records_to_context(all_chunks)

    weak = [r["angle"] for r in ordered_results if r.get("coverage", 100) < 40]
    failed = [r["angle"] for r in ordered_results if not r.get("chunk_count")]

    draft = ask(
        SYS_SYNTHESIS,

        f"Topic: {trend}\n\n"
        f"Subagent summaries:\n{summaries_text}\n\n"
        f"All deduplicated evidence chunks ({len(all_chunks)} total):\n{chunk_json}\n\n"
        f"Failed angles (no evidence): {', '.join(failed) if failed else '(none)'}\n"
        f"Weak angles (<40% coverage): {', '.join(weak) if weak else '(none)'}\n\n"
        "Produce a comprehensive markdown report.\n\n"
        f"{REPORT_STRUCTURE_REQUIREMENTS}\n\n"
        "Additional requirements:\n"
        "- Aim for a genuinely thorough report when the evidence supports it; do not compress away nuance just to be brief\n"
        "- Every non-obvious factual claim must have inline citation [S<source_id>:C<chunk_id>]\n"
        "- Prefer paragraphs that synthesize multiple sources instead of one-source-at-a-time dumping\n"
        "- Explain why the evidence matters, not just what it says\n"
        "- Include chronology, mechanism, and comparison where those strengthen the argument\n"
        "- Use tables for structured comparisons where useful\n"
        "- Use `---` separators between major sections\n"
        "- Flag any speculation explicitly\n"
        "- Acknowledge evidence gaps honestly\n"
        "- Do not include a source in the Sources section unless it is actually cited in the body",
        model=SYNTHESIS_MODEL,
        max_tokens=int(REPORT_POLICY["synthesis_max_tokens"]),
    )
    round_dir = _round_dir(run_dir, research_round)
    _write_text(round_dir / "draft.md", draft)
    _write_json(round_dir / "evidence.json", all_chunks)
    return draft, chunk_json, all_chunks

# ══════════════════════════════════════════════
# Step 4: Sufficiency evaluation — lead agent re-planning loop
# ══════════════════════════════════════════════

def evaluate_sufficiency(trend, draft, subagent_results, chunk_json, run_dir: Path, research_round: int):
    """Lead agent evaluates if the draft is sufficient or needs more research.

    This is the re-planning loop from Anthropic's architecture: after synthesis,
    the LeadResearcher decides whether to spawn additional subagents for gaps.
    """
    coverage_summary = "\n".join(
        f"- {r['angle']}: {r.get('coverage', '?')}% coverage, {r.get('chunk_count', len(r.get('chunks', [])))} chunks"
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
        "3. Did the draft reveal a NEW angle not in the original decomposition?\n"
        "4. Does the draft meaningfully address counterevidence and alternative explanations?\n"
        "5. Are any major claims under-cited or supported by only thin evidence?\n\n"
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
    gap_tasks = [
        _normalize_subagent_task(task, index + 1, trend, "moderate")
        for index, task in enumerate(result.get("gaps", []))
    ]
    _write_json(
        _round_dir(run_dir, research_round) / "sufficiency.json",
        {
            "sufficient": result.get("sufficient", True),
            "gaps": gap_tasks,
        },
    )
    return result.get("sufficient", True), gap_tasks

# ══════════════════════════════════════════════
# Step 5: CitationAgent — dedicated citation verification
# ══════════════════════════════════════════════

def verify_citations(trend, draft, chunk_json, run_dir: Path):
    """Dedicated CitationAgent that verifies every citation maps to real evidence.

    Matches Anthropic's architecture where a separate CitationAgent processes
    documents and the research report to identify specific locations for citations.
    """
    verification = ask(
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
    _write_text(run_dir / "citation-verification.md", verification)
    return verification

# ══════════════════════════════════════════════
# Step 6: Revision — final report incorporating all feedback
# ══════════════════════════════════════════════

def revise(trend, draft, citation_report, chunk_json, run_dir: Path):
    """Produce the final report incorporating citation verification feedback."""
    final_report = ask(
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
        f"6. Maintain this full structure:\n{REPORT_STRUCTURE_REQUIREMENTS}\n"
        "7. Preserve analytical depth, nuance, chronology, and counterevidence where they are supported\n"
        "8. **Bold** key statistics, use tables where appropriate\n"
        "9. Explicitly flag remaining speculation with qualifiers like "
        "\"evidence suggests\" or \"it appears that\"\n"
        "10. Use `---` separators between major sections\n"
        "11. Do not leave placeholder headings or generic filler",
        model=REVISION_MODEL,
        max_tokens=int(REPORT_POLICY["revision_max_tokens"]),
    )
    _write_text(run_dir / "final-report.md", final_report)
    return final_report

# ══════════════════════════════════════════════
# Orchestration: full multi-agent pipeline with re-planning
# ══════════════════════════════════════════════

def generate_report(
    conn,
    trend,
    *,
    persist_report: bool = True,
    publish_to_github: bool = True,
    write_local_post: bool = True,
):
    """Full pipeline matching Anthropic's multi-agent research architecture.

    LeadResearcher (extended thinking, effort scaling)
      → Parallel Subagents (OODA retrieval, broad-to-narrow)
      → Synthesis
      → Sufficiency evaluation (re-planning loop)
      → CitationAgent (dedicated verification)
      → Revision
    """

    # ── Step 1: Lead agent decomposes with extended thinking ──
    run_dir = _report_run_dir_for_trend(trend)
    log.info("Step 1: LeadResearcher decomposing topic with extended thinking...")
    plan = decompose_topic(trend)
    tasks = plan["tasks"]
    complexity = plan["complexity"]
    _persist_lead_plan(conn, run_dir, trend, plan)

    all_subagent_results = []

    for research_round in range(MAX_RESEARCH_ROUNDS):
        # ── Step 2: Parallel subagent research (OODA retrieval) ──
        round_label = f"Round {research_round + 1}"
        log.info("Step 2 (%s): Running %d subagents in parallel...", round_label, len(tasks))
        results = run_subagents(trend, tasks, run_dir, research_round + 1)
        all_subagent_results.extend(results)

        # ── Step 3: Synthesis ──
        log.info("Step 3 (%s): Synthesizing %d subagent outputs...", round_label, len(all_subagent_results))
        draft, chunk_json, all_chunks = synthesize(trend, all_subagent_results, run_dir, research_round + 1)

        # ── Step 4: Sufficiency evaluation (re-planning) ──
        if research_round < MAX_RESEARCH_ROUNDS - 1:
            log.info("Step 4 (%s): LeadResearcher evaluating sufficiency...", round_label)
            sufficient, gap_tasks = evaluate_sufficiency(
                trend,
                draft,
                all_subagent_results,
                chunk_json,
                run_dir,
                research_round + 1,
            )
            if sufficient or not gap_tasks:
                log.info("LeadResearcher: research sufficient, proceeding to citation verification")
                break
            log.info("LeadResearcher: found %d gaps, spawning additional subagents", len(gap_tasks))
            _write_json(_round_dir(run_dir, research_round + 1) / "gap-plan.json", {"tasks": gap_tasks})
            tasks = gap_tasks  # next round researches the gaps
        else:
            log.info("Max research rounds reached, proceeding to citation verification")

    # ── Step 5: CitationAgent ──
    log.info("Step 5: CitationAgent verifying citations...")
    citation_report = verify_citations(trend, draft, chunk_json, run_dir)

    # ── Step 6: Revision ──
    log.info("Step 6: Final revision incorporating citation feedback...")
    final_report = revise(trend, draft, citation_report, chunk_json, run_dir)

    # ── Save ──
    created_at = datetime.now(UTC)
    report_category = _report_category(trend, final_report)
    github_post_path = _report_post_relative_path(trend, created_at)
    github_post_content = _report_post_content(
        trend,
        final_report,
        created_at=created_at,
        category=report_category,
    )
    local_post_path = ROOT / github_post_path
    if write_local_post:
        _write_text(local_post_path, github_post_content)

    report_summary = _report_summary(final_report)
    github_url = ""
    github_content_url = ""
    github_branch = ""
    github_base_branch = GITHUB_BRANCH
    github_publish_error = ""
    discord_notify_error = ""
    if publish_to_github and GITHUB_TOKEN and GITHUB_REPO:
        try:
            publish_result = _publish_report_post_to_github(
                github_post_path,
                github_post_content,
                title=trend,
                summary=report_summary,
                category=report_category,
                created_at=created_at,
            )
            github_url = str(publish_result.get("pr_url") or "").strip()
            github_content_url = str(publish_result.get("content_url") or "").strip()
            github_branch = str(publish_result.get("branch") or "").strip()
            github_base_branch = str(publish_result.get("base_branch") or GITHUB_BRANCH).strip() or GITHUB_BRANCH
            discord_notify_error = str(publish_result.get("discord_notify_error") or "").strip()
        except Exception as exc:
            github_publish_error = str(exc)
            log.error("GitHub report publish failed for %s: %s", github_post_path, exc, exc_info=True)
            raise
    elif not publish_to_github:
        github_publish_error = "github_publish_disabled"
    else:
        github_publish_error = "github_not_configured"
        log.warning(
            "Skipping GitHub report publish for %s because GITHUB_TOKEN or GITHUB_REPO is not configured",
            github_post_path,
        )

    metadata_obj = {
        "complexity": complexity,
        "angles": [r["angle"] for r in all_subagent_results],
        "total_chunks": len(all_chunks),
        "research_rounds": research_round + 1,
        "report_run_dir": str(run_dir),
        "summary": report_summary,
        "category": report_category,
        "github_path": github_post_path,
        "github_repo": GITHUB_REPO,
        "github_branch": github_branch or None,
        "github_base_branch": github_base_branch,
        "url": github_url,
        "github_content_url": github_content_url or None,
        "github_publish_error": github_publish_error or None,
        "discord_notify_error": discord_notify_error or None,
        "models": {
            "lead": LEAD_MODEL,
            "eval": EVAL_MODEL,
            "summary": SUMMARY_MODEL,
            "synthesis": SYNTHESIS_MODEL,
            "citation": CITATION_MODEL,
            "revision": REVISION_MODEL,
            "signal": SIGNAL_MODEL,
        },
        "report_policy": REPORT_POLICY,
    }
    if persist_report:
        metadata = json.dumps(metadata_obj)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO reports (title, content, metadata) VALUES (%s, %s, %s::jsonb)",
                        (trend, final_report, metadata))
            conn.commit()

    log.info(
        "Report generated: %s github=%s persist=%s (%d chunks, %d angles, %d rounds)",
        local_post_path if write_local_post else "(local write skipped)",
        github_url or "not_published",
        persist_report,
        len(all_chunks),
        len(all_subagent_results),
        research_round + 1,
    )
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

    # Determine since_ts with an overlap window so late-arriving stories are re-checked.
    since_ts = None
    last_completed = load_state(conn, "last_ingest_completed_at")
    if last_completed:
        try:
            watermark = _compute_overlap_watermark(last_completed, RSS_OVERLAP_SECONDS)
            if watermark is not None:
                since_ts = watermark.timestamp()
                log.info(
                    "Fetching RSS items newer_than=%s (last_completed=%s overlap=%ss)",
                    int(since_ts),
                    last_completed,
                    RSS_OVERLAP_SECONDS,
                )
        except Exception as e:
            log.warning("Could not parse last_ingest_completed_at %r: %s — fetching all stories", last_completed, e)

    for item in fetch_rss(since_ts=since_ts):
        candidates_found += 1
        articles_extracted += 1
        item.update(build_source_dedupe_values(item))
        dedupe_key = item["key"]
        canonical_url = item.get("canonical_url", "")
        existing_id, existing_reason = find_existing_source(conn, dedupe_key, item.get("url_hash", ""), item.get("content_hash", ""))
        if existing_id is None:
            sid = store_source(conn, item, "rss")
            log.info("Ingest decision=new source_type=rss dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)
            chunk_and_embed(conn, sid, item["content"])
            new += 1
        elif existing_reason:
            duplicates += 1
            log.info("Ingest decision=duplicate source_type=rss dedupe_key=%s canonical_url=%s duplicate_by=%s", dedupe_key, canonical_url, existing_reason)
        else:
            skipped += 1
            log.info("Ingest decision=skipped source_type=rss dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)

    for name, cid in parse_youtube(ROOT / "feeds" / "youtube.md"):
        youtube_state_key = _youtube_channel_state_key(cid)
        last_published_raw = load_state(conn, youtube_state_key)
        published_after = None
        try:
            published_after = _compute_overlap_watermark(last_published_raw, YOUTUBE_OVERLAP_SECONDS)
        except Exception as e:
            log.warning("Could not parse %s=%r: %s — fetching full channel feed", youtube_state_key, last_published_raw, e)
        yt_items, discovery_failed, counters, _latest_published_at = fetch_youtube(name, cid, published_after=published_after)
        for key, value in counters.items():
            youtube_counters[key] += value
        if discovery_failed:
            youtube_discovery_failures += 1
            continue
        max_processed_published_at = None
        for item in yt_items:
            candidates_found += 1
            item.update(build_source_dedupe_values(item))
            dedupe_key = item["key"]
            canonical_url = item.get("canonical_url", "")
            existing_id, existing_reason = find_existing_source(conn, dedupe_key, item.get("url_hash", ""), item.get("content_hash", ""))
            if existing_id is None:
                sid = store_source(conn, item, "youtube")
                log.info("Ingest decision=new source_type=youtube dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)
                chunk_and_embed(conn, sid, item["content"])
                new += 1
                item_published_at = _parse_iso_datetime(item.get("published_at"))
                if item_published_at and (max_processed_published_at is None or item_published_at > max_processed_published_at):
                    max_processed_published_at = item_published_at
            elif existing_reason:
                duplicates += 1
                log.info("Ingest decision=duplicate source_type=youtube dedupe_key=%s canonical_url=%s duplicate_by=%s", dedupe_key, canonical_url, existing_reason)
                item_published_at = _parse_iso_datetime(item.get("published_at"))
                if item_published_at and (max_processed_published_at is None or item_published_at > max_processed_published_at):
                    max_processed_published_at = item_published_at
            else:
                skipped += 1
                log.info("Ingest decision=skipped source_type=youtube dedupe_key=%s canonical_url=%s", dedupe_key, canonical_url)
        if max_processed_published_at is not None:
            save_state(conn, youtube_state_key, max_processed_published_at.isoformat())

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
    return run_detect_impl(
        conn,
        min_new_sources=min_new_sources,
        backfill_days=backfill_days or _bertrend_lookback_days(),
        backfill_limit=backfill_limit,
        load_state_fn=load_state,
        count_recent_embedded_chunks_fn=_count_recent_embedded_chunks,
        run_backfill_fn=run_backfill,
        detect_trends_fn=detect_trends,
        embed_fn=embed,
    )


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
            "FROM trend_candidates WHERE status IN ('pending', 'needs_more_evidence') "
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
        if passes_report_gate(
            final_score=eff_score,
            source_diversity=src_div,
            min_score=min_score,
            min_sources=min_sources,
        ):
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
                f"UPDATE trend_candidates SET status = 'needs_more_evidence' WHERE id IN ({placeholders})",
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
    trend_vecs = embed([trend])
    if trend_vecs and trend_vecs[0]:
        update_baseline(conn, trend, trend_vecs[0], source_count=src_div)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE trend_candidates SET status = 'reported' WHERE id = %s",
            (candidate_id,),
        )
    conn.commit()


def run_rescore(conn, *, limit: int = 0, batch_size: int = 100, statuses: list[str] | None = None):
    return run_rescore_impl(
        conn,
        limit=limit,
        batch_size=batch_size,
        statuses=statuses,
        embed_fn=embed,
    )


def _runtime_summary_payload(*, llm_usage: dict) -> dict:
    return {
        "llm_usage": llm_usage,
    }


def _emit_runtime_summary(step: str, summary: dict | None) -> None:
    llm_usage = ((summary or {}).get("llm_usage") or {}) if isinstance(summary, dict) else {}
    print(f"RUN_STEP={step}")
    print(f"RUN_LLM_CALLS={int(llm_usage.get('llm_calls', 0) or 0)}")
    print(f"RUN_PROMPT_TOKENS={int(llm_usage.get('prompt_tokens', 0) or 0)}")
    print(f"RUN_COMPLETION_TOKENS={int(llm_usage.get('completion_tokens', 0) or 0)}")
    print(f"RUN_CACHED_PROMPT_TOKENS={int(llm_usage.get('cached_prompt_tokens', 0) or 0)}")
    print(f"RUN_REASONING_TOKENS={int(llm_usage.get('reasoning_tokens', 0) or 0)}")
    print(f"RUN_TOTAL_TOKENS={int(llm_usage.get('total_tokens', 0) or 0)}")
    print(f"RUN_LLM_COST_USD={float(llm_usage.get('llm_cost_usd', 0.0) or 0.0):.6f}")
    print(f"RUN_UNPRICED_CALLS={int(llm_usage.get('unpriced_calls', 0) or 0)}")
    print(
        "RUN_UNPRICED_MODELS="
        + json.dumps(sorted(llm_usage.get("unpriced_models") or []), ensure_ascii=False)
    )


def main():
    parser = argparse.ArgumentParser(description="Football research pipeline")
    parser.add_argument(
        "--step",
        choices=["ingest", "backfill", "detect", "rescore", "report", "all"],
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
        default=int(INGEST_POLICY.get("detect_min_new_sources", 0)),
        help="Skip detect when latest ingest inserted fewer than this many new sources (default: ingest_policy_config.json value)",
    )
    parser.add_argument(
        "--rescore-limit",
        type=int,
        default=0,
        help="Maximum number of historical trend candidates to rescore (default: 0 = all matches)",
    )
    parser.add_argument(
        "--rescore-batch-size",
        type=int,
        default=100,
        help="Batch size for embedding/rescoring historical trend candidates (default: 100)",
    )
    parser.add_argument(
        "--rescore-statuses",
        default="",
        help="Comma-separated trend_candidate statuses to rescore (default: all statuses)",
    )
    parser.add_argument(
        "--allow-report-after-detect",
        action="store_true",
        help="When using --step all, also run report in the same process (disabled by default)",
    )
    args = parser.parse_args()

    _validate_required_env(args.step)

    conn = _connect_db()
    started_at = utc_now()
    db_run = None
    self_logging_enabled = (os.environ.get("PIPELINE_DISABLE_SELF_LOGGING", "").strip().lower() not in {"1", "true", "yes"})
    parent_run_id_raw = (os.environ.get("PIPELINE_PARENT_RUN_ID") or "").strip()
    parent_run_id = int(parent_run_id_raw) if parent_run_id_raw.isdigit() else None
    trigger_source = (os.environ.get("PIPELINE_TRIGGER_SOURCE") or "cli").strip() or "cli"
    llm_usage = {}
    usage_tracker = None
    try:
        _ensure_schema(conn)
        if self_logging_enabled:
            db_run = start_run(
                conn,
                step=args.step,
                trigger_source=trigger_source,
                parent_run_id=parent_run_id,
                started_at=started_at,
            )
            conn.commit()

        with llm_usage_tracking() as usage_tracker:
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
            elif args.step == "rescore":
                run_rescore(
                    conn,
                    limit=args.rescore_limit,
                    batch_size=args.rescore_batch_size,
                    statuses=_parse_rescore_statuses(args.rescore_statuses),
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
            llm_usage = usage_tracker.summary()
            summary = _runtime_summary_payload(llm_usage=llm_usage)
            _emit_runtime_summary(args.step, summary)
            if db_run is not None:
                finish_run(
                    conn,
                    run=db_run,
                    status="success",
                    finished_at=utc_now(),
                    exit_code=0,
                    summary=summary,
                )
                conn.commit()
    except BaseException as exc:
        if usage_tracker is not None:
            llm_usage = usage_tracker.summary()
        summary = _runtime_summary_payload(llm_usage=llm_usage)
        _emit_runtime_summary(args.step, summary)
        if db_run is not None:
            exit_code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
            finish_run(
                conn,
                run=db_run,
                status="failed",
                finished_at=utc_now(),
                exit_code=exit_code,
                summary=summary,
            )
            conn.commit()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
