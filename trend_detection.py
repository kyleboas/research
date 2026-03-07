"""BERTrend-inspired weak signal detection for football tactics trends.

Implements the core algorithm from:
  BERTrend: Neural Topic Modeling for Emerging Trends Detection
  (Boutaleb et al., 2024 — arXiv:2411.05930)

Pipeline:
  1. Pull chunk embeddings from pgvector grouped by time window
  2. Cluster embeddings per window using HDBSCAN (fine-grained topics)
  3. Merge topics across windows via cosine similarity threshold
  4. Track popularity with exponential decay for inactive topics
  5. Classify signals as noise / weak / strong using rolling percentile thresholds
  6. Feed algorithmic signals into LLM for human-readable trend descriptions
"""

import json
import logging
import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.feature_extraction.text import TfidfVectorizer

log = logging.getLogger("research")

# ── Defaults (overridden by config.json bertrend section) ────────────────────

DEFAULT_CONFIG = {
    "window_days": 1,           # Time slice granularity (days per window)
    "lookback_days": 14,        # How far back to analyze
    "min_cluster_size": 3,      # HDBSCAN: minimum docs to form a topic
    "merge_threshold": 0.7,     # Cosine similarity threshold for cross-window topic merging
    "decay_lambda": 0.01,       # Exponential decay factor for inactive topics
    "noise_percentile": 10,     # P10 — below this = noise
    "weak_percentile": 50,      # P50 — below this = weak, above = strong
    "rolling_window_days": 7,   # Rolling window for percentile calculation
    "top_k_signals": 10,        # Max signals to return
    "tfidf_top_n": 8,           # Number of keywords per topic
}


def _load_config(cfg_path=None):
    """Load bertrend config from config.json, merged with defaults."""
    config = dict(DEFAULT_CONFIG)
    if cfg_path:
        try:
            with open(cfg_path) as f:
                file_cfg = json.load(f).get("bertrend", {})
            config.update({k: v for k, v in file_cfg.items() if k in DEFAULT_CONFIG})
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return config


# ══════════════════════════════════════════════
# Step 1: Pull embeddings from pgvector by time window
# ══════════════════════════════════════════════

def _fetch_chunks_by_window(conn, lookback_days, window_days):
    """Fetch chunks with embeddings, grouped into time windows.

    Returns list of (window_start, window_end, [(chunk_id, source_id, content, embedding), ...])
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.source_id, c.content, c.embedding::text, s.created_at "
            "FROM chunks c JOIN sources s ON c.source_id = s.id "
            "WHERE s.created_at > NOW() - INTERVAL '%s days' "
            "AND c.embedding IS NOT NULL "
            "ORDER BY s.created_at",
            (lookback_days,),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    # Parse embedding vectors from pgvector text format
    parsed = []
    for chunk_id, source_id, content, emb_text, created_at in rows:
        vec = [float(x) for x in emb_text.strip("[]").split(",")]
        parsed.append((chunk_id, source_id, content, vec, created_at))

    # Group into time windows
    earliest = min(r[4] for r in parsed)
    windows = []
    window_start = earliest.replace(hour=0, minute=0, second=0, microsecond=0)

    while window_start < datetime.now(UTC):
        window_end = window_start + timedelta(days=window_days)
        window_chunks = [
            (r[0], r[1], r[2], r[3])
            for r in parsed
            if window_start <= r[4] < window_end
        ]
        if window_chunks:
            windows.append((window_start, window_end, window_chunks))
        window_start = window_end

    return windows


# ══════════════════════════════════════════════
# Step 2: Cluster embeddings per time window (HDBSCAN)
# ══════════════════════════════════════════════

def _cluster_window(chunks, min_cluster_size):
    """Cluster chunk embeddings within a single time window using HDBSCAN.

    Returns list of topics: [{
        'chunk_ids': [...],
        'source_ids': set(...),
        'texts': [...],
        'centroid': np.array,
        'doc_count': int,
    }]
    """
    if len(chunks) < min_cluster_size:
        return []

    embeddings = np.array([c[3] for c in chunks])

    # HDBSCAN with fine-grained settings per BERTrend paper:
    # small min_cluster_size to catch weak signals early
    clusterer = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=1,
        metric="cosine",
    )
    labels = clusterer.fit_predict(embeddings)

    # Group chunks by cluster label (skip noise label -1)
    clusters = defaultdict(list)
    for i, label in enumerate(labels):
        if label >= 0:
            clusters[label].append(i)

    topics = []
    for label, indices in clusters.items():
        cluster_embeddings = embeddings[indices]
        centroid = cluster_embeddings.mean(axis=0)
        # Normalize centroid for cosine comparison
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm

        topics.append({
            "chunk_ids": [chunks[i][0] for i in indices],
            "source_ids": set(chunks[i][1] for i in indices),
            "texts": [chunks[i][2] for i in indices],
            "centroid": centroid,
            "doc_count": len(indices),
        })

    return topics


# ══════════════════════════════════════════════
# Step 3: Extract topic keywords via c-TF-IDF
# ══════════════════════════════════════════════

def _extract_keywords(topics, top_n=8):
    """Extract representative keywords for each topic using TF-IDF."""
    if not topics:
        return topics

    # Build per-topic concatenated documents
    topic_docs = [" ".join(t["texts"]) for t in topics]

    try:
        vectorizer = TfidfVectorizer(
            max_features=5000,
            stop_words="english",
            max_df=0.9,
            min_df=1,
        )
        tfidf_matrix = vectorizer.fit_transform(topic_docs)
        feature_names = vectorizer.get_feature_names_out()

        for i, topic in enumerate(topics):
            row = tfidf_matrix[i].toarray().flatten()
            top_indices = row.argsort()[-top_n:][::-1]
            topic["keywords"] = [feature_names[j] for j in top_indices if row[j] > 0]
    except ValueError:
        # Not enough documents for TF-IDF
        for topic in topics:
            topic["keywords"] = []

    return topics


# ══════════════════════════════════════════════
# Step 4: Merge topics across time windows
# ══════════════════════════════════════════════

class TopicTracker:
    """Tracks topics across time windows with merging and popularity decay.

    Implements BERTrend's online topic merging:
    - New window topics are matched to existing cumulative topics by cosine similarity
    - Matched topics are merged (popularity incremented)
    - Unmatched topics become new entries
    - Unupdated topics undergo exponential decay
    """

    def __init__(self, merge_threshold=0.7, decay_lambda=0.01):
        self.merge_threshold = merge_threshold
        self.decay_lambda = decay_lambda
        self.topics = {}  # topic_id -> topic state
        self._next_id = 0

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def update(self, window_start, window_end, window_topics):
        """Process a new time window's topics against the cumulative set.

        Returns list of (topic_id, action) where action is 'new', 'merged', or 'decayed'.
        """
        actions = []

        if not self.topics:
            # First window — all topics are new
            for wt in window_topics:
                tid = self._new_id()
                self.topics[tid] = {
                    "centroid": wt["centroid"],
                    "keywords": wt.get("keywords", []),
                    "popularity": float(wt["doc_count"]),
                    "doc_count": wt["doc_count"],
                    "total_docs": wt["doc_count"],
                    "first_seen": window_start,
                    "last_updated": window_end,
                    "update_count": 1,
                    "all_chunk_ids": list(wt["chunk_ids"]),
                    "all_source_ids": set(wt["source_ids"]),
                    "popularity_history": [(window_end, float(wt["doc_count"]))],
                }
                actions.append((tid, "new"))
            return actions

        # Match new window topics to existing topics via cosine similarity
        if window_topics:
            existing_ids = list(self.topics.keys())
            existing_centroids = np.array([self.topics[tid]["centroid"] for tid in existing_ids])
            new_centroids = np.array([wt["centroid"] for wt in window_topics])

            sim_matrix = cosine_similarity(new_centroids, existing_centroids)

            matched_existing = set()
            matched_new = set()

            # Greedy matching: best similarity first
            while True:
                if sim_matrix.size == 0:
                    break
                max_idx = np.unravel_index(sim_matrix.argmax(), sim_matrix.shape)
                max_sim = sim_matrix[max_idx]

                if max_sim < self.merge_threshold:
                    break

                new_idx, existing_idx = max_idx
                if new_idx in matched_new or existing_idx in matched_existing:
                    sim_matrix[new_idx, existing_idx] = -1
                    continue

                # Merge: update existing topic
                tid = existing_ids[existing_idx]
                wt = window_topics[new_idx]
                topic = self.topics[tid]

                # Update centroid (weighted average)
                old_weight = topic["total_docs"]
                new_weight = wt["doc_count"]
                combined = old_weight + new_weight
                topic["centroid"] = (
                    topic["centroid"] * old_weight + wt["centroid"] * new_weight
                ) / combined
                # Re-normalize
                norm = np.linalg.norm(topic["centroid"])
                if norm > 0:
                    topic["centroid"] = topic["centroid"] / norm

                # Update popularity: p_k_t' = p_k_(t'-1) + |D_k_t'|
                topic["popularity"] = topic["popularity"] + float(wt["doc_count"])
                topic["doc_count"] = wt["doc_count"]
                topic["total_docs"] = combined
                topic["last_updated"] = window_end
                topic["update_count"] += 1
                topic["all_chunk_ids"].extend(wt["chunk_ids"])
                topic["all_source_ids"].update(wt["source_ids"])
                topic["popularity_history"].append((window_end, topic["popularity"]))

                # Merge keywords (keep unique, prefer higher-ranked)
                seen = set(topic["keywords"])
                for kw in wt.get("keywords", []):
                    if kw not in seen:
                        topic["keywords"].append(kw)
                        seen.add(kw)

                matched_existing.add(existing_idx)
                matched_new.add(new_idx)
                actions.append((tid, "merged"))

                sim_matrix[new_idx, existing_idx] = -1

            # New topics that didn't match anything
            for i, wt in enumerate(window_topics):
                if i not in matched_new:
                    tid = self._new_id()
                    self.topics[tid] = {
                        "centroid": wt["centroid"],
                        "keywords": wt.get("keywords", []),
                        "popularity": float(wt["doc_count"]),
                        "doc_count": wt["doc_count"],
                        "total_docs": wt["doc_count"],
                        "first_seen": window_start,
                        "last_updated": window_end,
                        "update_count": 1,
                        "all_chunk_ids": list(wt["chunk_ids"]),
                        "all_source_ids": set(wt["source_ids"]),
                        "popularity_history": [(window_end, float(wt["doc_count"]))],
                    }
                    actions.append((tid, "new"))

        # Exponential decay for topics NOT updated in this window
        for tid, topic in self.topics.items():
            if topic["last_updated"] < window_end and tid not in {a[0] for a in actions}:
                delta_days = (window_end - topic["last_updated"]).total_seconds() / 86400
                # p_k_t' = p_k_(t'-1) * e^(-lambda * delta_t^2)
                decay = math.exp(-self.decay_lambda * delta_days * delta_days)
                topic["popularity"] = topic["popularity"] * decay
                topic["popularity_history"].append((window_end, topic["popularity"]))
                actions.append((tid, "decayed"))

        return actions


# ══════════════════════════════════════════════
# Step 5: Classify signals using rolling percentile thresholds
# ══════════════════════════════════════════════

def _classify_signals(tracker, rolling_window_days, noise_pct, weak_pct):
    """Classify all tracked topics as noise / weak / strong signals.

    Uses BERTrend's dynamic percentile thresholds:
    - P10 (noise_pct): below = noise
    - P50 (weak_pct): below = weak, above = strong
    Computed over a rolling window of recent popularity values.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=rolling_window_days)

    # Collect recent popularity values across all topics
    recent_popularities = []
    for topic in tracker.topics.values():
        for ts, pop in topic["popularity_history"]:
            if ts >= cutoff:
                recent_popularities.append(pop)

    if not recent_popularities:
        # No data in rolling window — everything is noise
        for topic in tracker.topics.values():
            topic["signal_class"] = "noise"
            topic["growth_rate"] = 0.0
        return

    p_noise = float(np.percentile(recent_popularities, noise_pct))
    p_weak = float(np.percentile(recent_popularities, weak_pct))

    for topic in tracker.topics.values():
        pop = topic["popularity"]

        if pop < p_noise:
            topic["signal_class"] = "noise"
        elif pop < p_weak:
            topic["signal_class"] = "weak"
        else:
            topic["signal_class"] = "strong"

        # Compute growth rate: compare current popularity to earliest in rolling window
        recent = [(ts, p) for ts, p in topic["popularity_history"] if ts >= cutoff]
        if len(recent) >= 2:
            earliest_pop = recent[0][1]
            if earliest_pop > 0:
                topic["growth_rate"] = (pop - earliest_pop) / earliest_pop
            else:
                topic["growth_rate"] = float("inf") if pop > 0 else 0.0
        else:
            topic["growth_rate"] = 0.0


# ══════════════════════════════════════════════
# Step 6: Run full BERTrend pipeline
# ══════════════════════════════════════════════

def run_bertrend_detection(conn, config=None, cfg_path=None):
    """Run the full BERTrend-inspired trend detection pipeline.

    Returns list of signal dicts sorted by relevance (weak + growing first),
    each containing: signal_class, keywords, popularity, growth_rate,
    doc_count, source_ids, chunk_ids, first_seen, last_updated.
    """
    cfg = _load_config(cfg_path)
    if config:
        cfg.update(config)

    log.info("BERTrend detection: lookback=%dd, windows=%dd, merge=%.2f",
             cfg["lookback_days"], cfg["window_days"], cfg["merge_threshold"])

    # Step 1: Fetch embeddings by time window
    windows = _fetch_chunks_by_window(conn, cfg["lookback_days"], cfg["window_days"])
    if not windows:
        log.info("BERTrend: no chunks with embeddings in lookback window")
        return []

    log.info("BERTrend: %d time windows, %d total chunks",
             len(windows), sum(len(w[2]) for w in windows))

    # Step 2-3: Cluster each window and extract keywords
    tracker = TopicTracker(
        merge_threshold=cfg["merge_threshold"],
        decay_lambda=cfg["decay_lambda"],
    )

    for window_start, window_end, chunks in windows:
        window_topics = _cluster_window(chunks, cfg["min_cluster_size"])
        window_topics = _extract_keywords(window_topics, cfg["tfidf_top_n"])
        log.info("BERTrend: window %s → %d topics from %d chunks",
                 window_start.strftime("%Y-%m-%d"), len(window_topics), len(chunks))

        # Step 4: Merge into cumulative tracker
        actions = tracker.update(window_start, window_end, window_topics)
        action_counts = defaultdict(int)
        for _, action in actions:
            action_counts[action] += 1
        log.info("BERTrend: merge results — new=%d merged=%d decayed=%d",
                 action_counts.get("new", 0),
                 action_counts.get("merged", 0),
                 action_counts.get("decayed", 0))

    # Step 5: Classify signals
    _classify_signals(
        tracker, cfg["rolling_window_days"],
        cfg["noise_percentile"], cfg["weak_percentile"],
    )

    # Build results — prioritize weak+growing and strong signals
    signals = []
    for tid, topic in tracker.topics.items():
        if topic["signal_class"] == "noise":
            continue

        signals.append({
            "topic_id": tid,
            "signal_class": topic["signal_class"],
            "keywords": topic.get("keywords", [])[:cfg["tfidf_top_n"]],
            "popularity": topic["popularity"],
            "growth_rate": topic.get("growth_rate", 0.0),
            "doc_count": topic["total_docs"],
            "update_count": topic["update_count"],
            "source_ids": list(topic["all_source_ids"]),
            "chunk_ids": topic["all_chunk_ids"],
            "first_seen": topic["first_seen"].isoformat(),
            "last_updated": topic["last_updated"].isoformat(),
        })

    # Sort: weak+growing signals first (most novel), then strong
    def _sort_key(s):
        # Weak signals with high growth are most interesting (emerging trends)
        # Strong signals are already established
        class_order = {"weak": 0, "strong": 1}
        return (class_order.get(s["signal_class"], 2), -s["growth_rate"], -s["popularity"])

    signals.sort(key=_sort_key)
    return signals[:cfg["top_k_signals"]]


# ══════════════════════════════════════════════
# Step 7: LLM-enhanced trend description from algorithmic signals
# ══════════════════════════════════════════════

def describe_signals_with_llm(conn, signals, ask_fn, past_topics=None):
    """Use LLM to generate human-readable trend descriptions from algorithmic signals.

    Takes the raw BERTrend signals and asks the LLM to synthesize them into
    actionable trend candidates with scores, using the actual source content
    as grounding.

    Args:
        conn: Database connection
        signals: List of signal dicts from run_bertrend_detection()
        ask_fn: The ask() function from main.py for LLM calls
        past_topics: List of already-covered report titles to avoid repeats

    Returns:
        List of trend candidate dicts compatible with the existing pipeline:
        [{"trend": str, "reasoning": str, "score": int, "source_titles": [str], "sources": [...]}]
    """
    if not signals:
        return []

    # Fetch source content for the top signals' chunks
    all_chunk_ids = []
    for s in signals:
        all_chunk_ids.extend(s["chunk_ids"][:20])  # Cap per signal to avoid huge context
    all_chunk_ids = list(set(all_chunk_ids))

    if not all_chunk_ids:
        return []

    # Fetch chunk content and source metadata
    placeholders = ",".join(["%s"] * len(all_chunk_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT c.id, c.content, s.id AS source_id, s.title, s.url "
            f"FROM chunks c JOIN sources s ON c.source_id = s.id "
            f"WHERE c.id IN ({placeholders})",
            all_chunk_ids,
        )
        chunk_data = {r[0]: {"content": r[1], "source_id": r[2], "title": r[3], "url": r[4]}
                      for r in cur.fetchall()}

    # Build signal summaries for the LLM
    signal_descriptions = []
    for i, s in enumerate(signals):
        chunks_for_signal = [chunk_data[cid] for cid in s["chunk_ids"][:20] if cid in chunk_data]
        source_titles = list({c["title"] for c in chunks_for_signal if c["title"]})
        excerpts = [c["content"][:300] for c in chunks_for_signal[:5]]

        signal_descriptions.append(
            f"Signal {i+1} ({s['signal_class']}, growth={s['growth_rate']:.1%}, "
            f"docs={s['doc_count']}, updates={s['update_count']}):\n"
            f"  Keywords: {', '.join(s['keywords'][:8])}\n"
            f"  Sources: {', '.join(source_titles[:5])}\n"
            f"  Excerpts:\n" + "\n".join(f"    - {e}..." for e in excerpts)
        )

    past_block = "\n".join(f"- {t}" for t in (past_topics or [])) if past_topics else "(none)"

    prompt = (
        f"Algorithmically detected signals from football tactics content:\n\n"
        f"{'---'.join(signal_descriptions)}\n\n"
        f"Already-covered topics (avoid repeating):\n{past_block}\n\n"
        "Each signal above was detected by clustering article embeddings over time and tracking "
        "which topic clusters are growing. 'Weak' signals are emerging trends not yet mainstream. "
        "'Strong' signals are established trends.\n\n"
        "For each signal, synthesize it into a clear tactical trend description. Focus on WEAK "
        "signals with high growth rates — these are the novel trends worth reporting on.\n\n"
        "Score each trend 0-100 where 100 = extremely novel and underreported.\n"
        "Include source_titles as exact titles from the signal data.\n\n"
        "Return ONLY valid JSON:\n"
        '{"candidates": ['
        '{"trend": "<10-20 word description>", "reasoning": "<why novel, referencing signal data>", '
        '"score": <0-100>, "source_titles": ["<exact title>"]}'
        ", ...]}"
    )

    text = ask_fn(
        "You are a football tactics analyst interpreting algorithmically detected trend signals. "
        "You specialize in identifying novel tactical patterns before they go mainstream. "
        "You have been given signals from a BERTrend-style weak signal detection system that "
        "clusters articles by semantic similarity and tracks which clusters are growing over time.",
        prompt,
    )

    candidates = _parse_json_safe(text).get("candidates", [])

    # Attach source metadata
    valid = []
    for c in candidates:
        if not (isinstance(c, dict) and c.get("trend") and isinstance(c.get("score"), int)):
            continue

        # Map source_titles to source records
        matched_sources = []
        seen_source_ids = set()
        for title in c.get("source_titles") or []:
            title = str(title).strip()
            for cd in chunk_data.values():
                if cd["title"] and cd["title"].strip() == title and cd["source_id"] not in seen_source_ids:
                    matched_sources.append({
                        "source_id": cd["source_id"],
                        "title": cd["title"],
                        "url": cd["url"] or "",
                    })
                    seen_source_ids.add(cd["source_id"])

        c["sources"] = matched_sources
        valid.append(c)

    return valid


def _parse_json_safe(text):
    """Extract JSON from LLM response text."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    block = re.search(r"[\[{].*[\]}]", stripped, re.DOTALL)
    if block:
        try:
            return json.loads(block.group())
        except json.JSONDecodeError:
            pass
    log.error("BERTrend LLM JSON parse failed: %r", text[:300])
    return {}


# ══════════════════════════════════════════════
# Database persistence for topic tracking state
# ══════════════════════════════════════════════

def save_topic_state(conn, tracker):
    """Persist the TopicTracker state to topic_snapshots table."""
    snapshot = {
        "next_id": tracker._next_id,
        "topics": {},
    }
    for tid, t in tracker.topics.items():
        snapshot["topics"][str(tid)] = {
            "centroid": t["centroid"].tolist(),
            "keywords": t["keywords"],
            "popularity": t["popularity"],
            "doc_count": t["doc_count"],
            "total_docs": t["total_docs"],
            "first_seen": t["first_seen"].isoformat(),
            "last_updated": t["last_updated"].isoformat(),
            "update_count": t["update_count"],
            "all_chunk_ids": t["all_chunk_ids"][-500:],  # Cap to avoid unbounded growth
            "all_source_ids": list(t["all_source_ids"]),
            "signal_class": t.get("signal_class", "noise"),
            "growth_rate": t.get("growth_rate", 0.0),
            "popularity_history": [
                (ts.isoformat(), p) for ts, p in t["popularity_history"][-50:]
            ],
        }

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO topic_snapshots (snapshot) VALUES (%s)",
            (json.dumps(snapshot),),
        )
    conn.commit()
    log.info("Saved topic snapshot: %d topics", len(tracker.topics))


def load_topic_state(conn):
    """Load the most recent TopicTracker state from topic_snapshots."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot FROM topic_snapshots ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()

    if not row:
        return None

    data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    tracker = TopicTracker()
    tracker._next_id = data.get("next_id", 0)

    for tid_str, t in data.get("topics", {}).items():
        tid = int(tid_str)
        tracker.topics[tid] = {
            "centroid": np.array(t["centroid"]),
            "keywords": t["keywords"],
            "popularity": t["popularity"],
            "doc_count": t["doc_count"],
            "total_docs": t["total_docs"],
            "first_seen": datetime.fromisoformat(t["first_seen"]),
            "last_updated": datetime.fromisoformat(t["last_updated"]),
            "update_count": t["update_count"],
            "all_chunk_ids": t["all_chunk_ids"],
            "all_source_ids": set(t["all_source_ids"]),
            "signal_class": t.get("signal_class", "noise"),
            "growth_rate": t.get("growth_rate", 0.0),
            "popularity_history": [
                (datetime.fromisoformat(ts), p) for ts, p in t.get("popularity_history", [])
            ],
        }

    log.info("Loaded topic snapshot: %d topics", len(tracker.topics))
    return tracker
