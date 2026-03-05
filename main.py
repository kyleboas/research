#!/usr/bin/env python3
"""Football research pipeline: ingest → detect trends → multi-agent deep research report.

Architecture mirrors Anthropic's production research system:
  LeadResearcher (extended thinking) → parallel Subagents (OODA retrieval)
  → Synthesis → Sufficiency evaluation → optional re-plan → CitationAgent → Revision
"""

import argparse, json, logging, os, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import anthropic, openai, psycopg

log = logging.getLogger("research")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

ROOT = Path(__file__).resolve().parent
GATEWAY = os.environ["CLOUDFLARE_GATEWAY_URL"].rstrip("/")
GATEWAY_TOKEN = os.environ["CLOUDFLARE_GATEWAY_TOKEN"]
TRANSCRIPT_KEY = os.environ["TRANSCRIPT_API_KEY"]
LEAD_MODEL = os.environ.get("CLAUDE_LEAD_MODEL", "claude-opus-4-6")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")

_gw_headers = {"cf-aig-authorization": f"Bearer {GATEWAY_TOKEN}"}
claude = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY", "cloudflare"),
    base_url=f"{GATEWAY}/anthropic",
    default_headers=_gw_headers,
)
oai = openai.OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY", "cloudflare"),
    base_url=f"{GATEWAY}/openai",
    default_headers=_gw_headers,
)

CITATION_FMT = "Cite every claim as [S<source_id>:C<chunk_id>]. Never cite IDs not in the provided context."

# ══════════════════════════════════════════════
# Feed parsing
# ══════════════════════════════════════════════

def parse_feeds(path):
    text = path.read_text()
    names = [m.group(1) for m in re.finditer(r"^-\s+\*\*(.+?)\*\*", text, re.M)]
    urls = [m.group(1) for m in re.finditer(r"^\s+-\s+Feed:\s*(\S+)", text, re.M)]
    return list(zip(names, urls))

def parse_youtube(path):
    text = path.read_text()
    names = [m.group(1) for m in re.finditer(r"^-\s+\*\*(.+?)\*\*", text, re.M)]
    cids = [m.group(1) for m in re.finditer(r"^\s+-\s+Channel ID:\s*(\S+)", text, re.M)]
    return list(zip(names, cids))

def strip_html(html):
    return re.sub(r"<[^>]+>", "", html).strip()

# ══════════════════════════════════════════════
# RSS ingestion
# ══════════════════════════════════════════════

NS = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/"}

def _txt(el):
    return (el.text or "").strip() if el is not None else ""

def _get(url, headers=None, timeout=15):
    req = Request(url, headers=headers or {"User-Agent": "ResearchBot/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()

def fetch_rss(name, url):
    try:
        xml_bytes = _get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)",
            "Accept": "application/rss+xml, application/atom+xml, */*",
        })
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        log.warning("Feed %s failed: %s", name, e)
        return []

    items = []
    if root.tag.endswith("feed"):  # Atom
        for entry in root.findall("atom:entry", NS)[:10]:
            link = entry.find("atom:link[@rel='alternate']", NS) or entry.find("atom:link", NS)
            href = (link.attrib.get("href", "") if link is not None else "").strip()
            content = strip_html(_txt(entry.find("atom:content", NS)) or _txt(entry.find("atom:summary", NS)))
            if content:
                items.append({"title": _txt(entry.find("atom:title", NS)), "url": href,
                              "content": content, "key": f"rss:{href or _txt(entry.find('atom:id', NS))}"})
    else:  # RSS 2.0
        for item in root.findall("./channel/item")[:10]:
            content = strip_html(_txt(item.find("content:encoded", NS)) or _txt(item.find("description")))
            item_url = _txt(item.find("link"))
            if content:
                items.append({"title": _txt(item.find("title")), "url": item_url,
                              "content": content, "key": f"rss:{_txt(item.find('guid')) or item_url}"})
    return items

# ══════════════════════════════════════════════
# YouTube ingestion
# ══════════════════════════════════════════════

YT_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

def fetch_youtube(name, channel_id):
    try:
        xml_bytes = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        log.warning("YouTube %s failed: %s", name, e)
        return []

    items = []
    for entry in root.findall("atom:entry", YT_NS)[:5]:
        vid = (entry.findtext("yt:videoId", default="", namespaces=YT_NS) or "").strip()
        if not vid:
            continue
        title = (entry.findtext("atom:title", default="", namespaces=YT_NS) or "").strip()
        try:
            turl = f"https://transcriptapi.com/api/v2/youtube/transcript?{urlencode({'video_url': f'https://www.youtube.com/watch?v={vid}'})}"
            data = json.loads(_get(turl, headers={"Authorization": f"Bearer {TRANSCRIPT_KEY}", "Accept": "application/json"}))
            transcript = ""
            for k in ("transcript", "text", "content"):
                v = data.get(k)
                if isinstance(v, str): transcript = v; break
                if isinstance(v, list): transcript = " ".join(p.get("text", "") for p in v if isinstance(p, dict)); break
        except Exception as e:
            log.warning("Transcript %s failed: %s", vid, e)
            continue
        if transcript.strip():
            items.append({"title": title, "url": f"https://www.youtube.com/watch?v={vid}",
                          "content": transcript.strip(), "key": f"yt:{channel_id}:{vid}"})
    return items

# ══════════════════════════════════════════════
# Storage & embedding
# ══════════════════════════════════════════════

def store_source(conn, item, source_type):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sources (source_type, source_key, title, url, content) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (source_key) DO NOTHING RETURNING id",
            (source_type, item["key"], item["title"], item["url"], item["content"]),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None

def embed(texts):
    return [d.embedding for d in oai.embeddings.create(model=EMBED_MODEL, input=texts).data]

def vec_literal(vec):
    return "[" + ",".join(str(v) for v in vec) + "]"

def chunk_and_embed(conn, source_id, text):
    words = text.split()
    if not words:
        return
    chunks = []
    for i in range(0, len(words), 160):
        chunk = " ".join(words[i:i + 200])
        if chunk.strip():
            chunks.append(chunk.strip())
        if i + 200 >= len(words):
            break
    if not chunks:
        return

    vectors = embed(chunks)
    with conn.cursor() as cur:
        for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            cur.execute(
                "INSERT INTO chunks (source_id, chunk_index, content, embedding) "
                "VALUES (%s, %s, %s, %s::vector) ON CONFLICT (source_id, chunk_index) DO NOTHING",
                (source_id, idx, chunk, vec_literal(vec)),
            )
        conn.commit()

# ══════════════════════════════════════════════
# Hybrid retrieval (semantic + keyword via RRF)
# ══════════════════════════════════════════════

def hybrid_search(conn, query, limit=20):
    qvec = embed([query])[0]
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
# Claude helpers
# ══════════════════════════════════════════════

def ask(system, user, model=None, max_tokens=4096):
    """Standard Claude call — system + user → text."""
    resp = claude.messages.create(
        model=model or MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text

def ask_thinking(system, user, budget_tokens=10000, max_tokens=16000):
    """Lead agent call with extended thinking enabled (Opus only).

    Extended thinking gives the model a scratchpad to reason before responding,
    matching Anthropic's LeadResearcher pattern for plan-then-act behavior.
    """
    resp = claude.messages.create(
        model=LEAD_MODEL,
        max_tokens=max_tokens,
        thinking={"type": "enabled", "budget_tokens": budget_tokens},
        messages=[{"role": "user", "content": f"<system>{system}</system>\n\n{user}"}],
    )
    # Return (thinking_text, response_text) — thinking is the scratchpad
    thinking = ""
    response = ""
    for block in resp.content:
        if block.type == "thinking":
            thinking = block.thinking
        elif block.type == "text":
            response = block.text
    return thinking, response

def parse_json(text):
    """Extract JSON from a text response."""
    match = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No JSON found in response: {text[:200]}")

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

def detect_trends(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, LEFT(content, 500) FROM sources "
            "WHERE created_at > NOW() - INTERVAL '7 days' ORDER BY created_at DESC LIMIT 100"
        )
        recent = cur.fetchall()
    if not recent:
        return None

    summaries = "\n".join(f"- {t}: {c}..." for t, c in recent)
    with conn.cursor() as cur:
        cur.execute("SELECT title FROM reports ORDER BY created_at DESC LIMIT 10")
        past = [r[0] for r in cur.fetchall()]
    past_block = "\n".join(f"- {t}" for t in past) if past else "(none)"

    try:
        text = ask(
            "You are a football tactics analyst spotting novel trends before they go mainstream.",
            f"Recent articles and transcripts:\n{summaries}\n\n"
            f"Already-covered topics (avoid repeating):\n{past_block}\n\n"
            "Identify the single most novel tactical or strategic trend being tried by football "
            "players or teams. Something new that hasn't been widely adopted yet.\n\n"
            'Return JSON: {{"trend": "<10-20 word description>", "reasoning": "<why novel>"}}'
        )
        return parse_json(text).get("trend")
    except Exception as e:
        log.warning("Trend detection failed: %s", e)
        return None

# ══════════════════════════════════════════════
# Step 1: LeadResearcher — decompose with extended thinking + effort scaling
# ══════════════════════════════════════════════

def decompose_topic(trend):
    """Lead agent uses extended thinking to reason about decomposition strategy.

    Implements Anthropic's effort scaling: the lead agent assesses complexity
    and calibrates the number of subagents and retrieval depth accordingly.
    """
    thinking, response = ask_thinking(
        "You are a LeadResearcher orchestrating a multi-agent deep research system. "
        "Your job is to decompose the research topic into non-overlapping subagent tasks "
        "with clear boundaries. Each subagent will run independently with its own context window.\n\n"
        "EFFORT SCALING RULES:\n"
        "- Simple fact-finding: 1-2 subagents, max_rounds=2, 3-5 search queries each\n"
        "- Moderate analysis: 3-4 subagents, max_rounds=3, 3-5 search queries each\n"
        "- Complex multi-faceted research: 5-7 subagents, max_rounds=5, 4-6 search queries each\n\n"
        "You MUST assess which complexity level applies and set parameters accordingly.",

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
                "You are evaluating retrieval sufficiency for a research subagent following "
                "the OODA loop (Observe-Orient-Decide-Act).\n\n"
                "OBSERVE: Review the chunks collected so far.\n"
                "ORIENT: Compare against the research objective — what's covered vs what's missing?\n"
                "DECIDE: Is evidence sufficient, or do we need another retrieval round?\n\n"
                "If more retrieval is needed, generate a query that is NARROWER and MORE SPECIFIC "
                "than previous queries — do not repeat broad searches.",

                f"Angle: {angle}\n"
                f"Objective: {objective}\n"
                f"Round: {round_num + 1}/{max_rounds}\n"
                f"Previous queries: {json.dumps(queries[:round_num + 1])}\n\n"
                f"Chunks collected ({len(all_chunks)} total):\n{chunk_json}\n\n"
                'Return JSON: {{"sufficient": true/false, "coverage_pct": 0-100, '
                '"gaps": ["specific gap 1", ...], "next_query": "narrower query" or null}}'
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

    if not all_chunks:
        return {"angle": angle, "summary": f"No evidence found for: {angle}",
                "chunks": [], "coverage": 0}

    # Write grounded summary for this angle
    chunk_json = chunks_to_context(list(all_chunks.values()))
    summary = ask(
        f"You are a focused research subagent. {CITATION_FMT}\n\n"
        "Stay strictly within your assigned boundaries. Do not speculate beyond "
        "what the evidence supports. If evidence is thin, say so explicitly.",

        f"Angle: {angle}\n"
        f"Objective: {objective}\n"
        f"Out of scope: {boundaries}\n\n"
        f"Evidence chunks:\n{chunk_json}\n\n"
        "Write a thorough, evidence-grounded analysis for this angle:\n"
        "- Lead with the strongest finding\n"
        "- Use inline citations [S<source_id>:C<chunk_id>] on every claim\n"
        "- Bold key statistics and figures\n"
        "- Note evidence quality and any limitations\n"
        "- Flag if evidence was insufficient for any part of the objective"
    )
    log.info("Subagent '%s' done: %d chunks, %d rounds", angle, len(all_chunks), round_num + 1)
    return {"angle": angle, "summary": summary, "chunks": list(all_chunks.values()),
            "coverage": eval_result.get("coverage_pct", 50) if 'eval_result' in dir() else 50}

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
        f"You are a synthesis editor merging multiple subagent research outputs into "
        f"one coherent, publication-quality research report. {CITATION_FMT}",

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
        "You are the LeadResearcher evaluating whether the synthesized research is sufficient "
        "or whether additional subagent research rounds are needed.\n\n"
        "Be critical but pragmatic. Only request additional research if there are SPECIFIC, "
        "ACTIONABLE gaps that more retrieval could realistically fill.",

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
        "You are a CitationAgent. Your SOLE job is to verify citations in a research report.\n\n"
        "For EVERY citation [S<source_id>:C<chunk_id>] in the report:\n"
        "1. Verify the source_id and chunk_id exist in the provided chunks\n"
        "2. Verify the cited claim is actually supported by that chunk's content\n"
        "3. Check for claims that SHOULD have citations but don't\n"
        "4. Check for fabricated/hallucinated citation IDs\n\n"
        "You must also verify the Sources section at the end lists accurate titles and URLs.",

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
    )

# ══════════════════════════════════════════════
# Step 6: Revision — final report incorporating all feedback
# ══════════════════════════════════════════════

def revise(trend, draft, citation_report, chunk_json):
    """Produce the final report incorporating citation verification feedback."""
    return ask(
        f"You are a revision editor producing the final research report. {CITATION_FMT}\n\n"
        "You have received a citation verification report from the CitationAgent. "
        "Apply every directive precisely. The final report must have zero citation errors.",

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
        "model": MODEL,
        "lead_model": LEAD_MODEL,
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

# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

def run_ingest(conn):
    new = 0
    for name, url in parse_feeds(ROOT / "feeds" / "rss.md"):
        for item in fetch_rss(name, url):
            sid = store_source(conn, item, "rss")
            if sid:
                chunk_and_embed(conn, sid, item["content"])
                new += 1
    for name, cid in parse_youtube(ROOT / "feeds" / "youtube.md"):
        for item in fetch_youtube(name, cid):
            sid = store_source(conn, item, "youtube")
            if sid:
                chunk_and_embed(conn, sid, item["content"])
                new += 1
    log.info("Ingested %d new sources", new)


def run_detect(conn):
    trend = detect_trends(conn)
    if trend:
        log.info("Detected trend: %s", trend)
        save_state(conn, "pending_trend", trend)
    else:
        log.info("No novel trend detected this run")


def run_report(conn):
    trend = load_state(conn, "pending_trend")
    if not trend:
        log.info("No pending trend found — skipping report")
        return
    log.info("Generating report for trend: %s", trend)
    generate_report(conn, trend)
    save_state(conn, "pending_trend", "")


def main():
    parser = argparse.ArgumentParser(description="Football research pipeline")
    parser.add_argument(
        "--step",
        choices=["ingest", "detect", "report", "all"],
        default="all",
        help="Pipeline step to run (default: all)",
    )
    args = parser.parse_args()

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        if args.step == "ingest":
            run_ingest(conn)
        elif args.step == "detect":
            run_detect(conn)
        elif args.step == "report":
            run_report(conn)
        else:
            run_ingest(conn)
            trend = detect_trends(conn)
            if trend:
                log.info("Detected trend: %s", trend)
                generate_report(conn, trend)
            else:
                log.info("No novel trend detected this run")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
