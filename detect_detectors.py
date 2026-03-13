import logging

from novelty_scoring import compute_novelty_score

log = logging.getLogger("research")


def detect_novel_tactical_patterns(conn, past_topics, *, embed_fn):
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

    corroborated = {key: value for key, value in action_groups.items() if len(value["source_ids"]) >= 2}
    log.info(
        "Tactical patterns: %d action groups, %d corroborated (2+ sources)",
        len(action_groups),
        len(corroborated),
    )
    if not corroborated:
        return []

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

    vectors = embed_fn(descriptions)
    if not vectors:
        log.warning("Tactical patterns: embed() returned empty for %d descriptions", len(descriptions))
        return []

    candidates = []
    for (key, group), desc, vec in zip(groups_list, descriptions, vectors):
        novelty = compute_novelty_score(conn, desc, vec, source_count=len(group["source_ids"]))
        if novelty < 0.3:
            continue

        score = int(min(100, novelty * 100))
        candidates.append(
            {
                "trend": desc,
                "reasoning": (
                    f"Novel tactical pattern detected: {group['actor']} performing {group['action']} "
                    f"across {len(group['source_ids'])} sources. "
                    f"Zones: {', '.join(list(group['zones'])[:3]) if group['zones'] else 'unspecified'}. "
                    f"Novelty score: {novelty:.2f}."
                ),
                "score": score,
                "source_titles": group["source_titles"][:5],
                "sources": [{"source_id": sid, "title": "", "url": ""} for sid in list(group["source_ids"])[:5]],
                "novelty_score": novelty,
                "source_diversity": len(group["source_ids"]),
                "pattern_ids": group["pattern_ids"],
                "detection_method": "tactical_pattern",
            }
        )

    candidates.sort(key=lambda candidate: -candidate["novelty_score"])
    below_threshold = len(corroborated) - len(candidates)
    log.info(
        "Tactical patterns: %d candidates above novelty threshold (0.3), %d below",
        len(candidates),
        below_threshold,
    )
    return candidates[:10]


def dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen_trends = set()
    deduped = []
    for candidate in sorted(candidates, key=lambda item: -item.get("score", 0)):
        trend_lower = candidate["trend"].lower().strip()
        is_dupe = False
        for seen in seen_trends:
            words_new = set(trend_lower.split())
            words_seen = set(seen.split())
            if len(words_new & words_seen) / max(1, len(words_new | words_seen)) > 0.6:
                is_dupe = True
                break
        if not is_dupe:
            seen_trends.add(trend_lower)
            deduped.append(candidate)
    return deduped


def detect_trends_llm_only(conn, past_topics, *, ask_fn, parse_json_fn) -> tuple[list[dict], bool]:
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

    def normalize_title(value: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())

    summaries = []
    for source_id, title, url, content in recent:
        source_title = (title or "Untitled source").strip()
        normalized_title = normalize_title(source_title)
        summaries.append(f"- {source_title}: {content}...")
        source_catalog.setdefault(source_title, []).append(
            {"source_id": source_id, "title": source_title, "url": url or ""}
        )
        normalized_catalog.setdefault(normalized_title, []).append(
            {"source_id": source_id, "title": source_title, "url": url or ""}
        )

    past_block = "\n".join(f"- {title}" for title in past_topics) if past_topics else "(none)"
    prompt_body = "Recent articles and transcripts:\n" + "\n".join(summaries) + "\n\n"
    log.info(
        "LLM-only fallback: sending %d sources, prompt ~%d chars, %d past topics excluded",
        len(recent),
        len(prompt_body),
        len(past_topics),
    )

    try:
        text = ask_fn(
            "You are a football tactics analyst spotting novel trends before they go mainstream.",
            prompt_body
            + f"Already-covered topics (avoid repeating):\n{past_block}\n\n"
            "Identify the top 5 most novel tactical or strategic trends being tried by football "
            "players or teams. Rank them by novelty — things not yet widely adopted get higher scores.\n\n"
            "Score each trend 0-100 where 100 = extremely novel and underreported, 0 = widely known.\n\n"
            "For each trend include source_titles as a list of exact titles from the provided source list that most strongly support the trend.\n\n"
            "Return ONLY valid JSON. No markdown. No code fences. No prose. Use double quotes.\n"
            'Format: {"candidates": ['
            '{"trend": "<10-20 word description>", "reasoning": "<why novel>", "score": <0-100>, "source_titles": ["<exact title>"]}'
            ', ...]}',
        )
        log.info("LLM-only trend detection raw response: %r", text)
        candidates = parse_json_fn(text).get("candidates", [])
        valid = []
        for candidate in candidates:
            if not (
                isinstance(candidate, dict)
                and candidate.get("trend")
                and isinstance(candidate.get("score"), int)
            ):
                continue

            matched_sources = []
            for title in candidate.get("source_titles") or []:
                query_title = str(title).strip()
                query_normalized = normalize_title(query_title)

                matched_sources.extend(source_catalog.get(query_title, []))
                matched_sources.extend(normalized_catalog.get(query_normalized, []))

                if query_normalized:
                    for known_normalized, known_sources in normalized_catalog.items():
                        if query_normalized in known_normalized or known_normalized in query_normalized:
                            matched_sources.extend(known_sources)

            deduped_sources = []
            seen_source_ids = set()
            for source in matched_sources:
                if source["source_id"] in seen_source_ids:
                    continue
                seen_source_ids.add(source["source_id"])
                deduped_sources.append(source)

            candidate["sources"] = deduped_sources
            valid.append(candidate)

        log.info(
            "LLM-only fallback: %d raw candidates from LLM, %d valid after filtering",
            len(candidates),
            len(valid),
        )
        return valid, False
    except Exception as exc:
        log.warning("LLM-only trend detection failed: %s", exc, exc_info=True)
        return [], True


def detect_trends(
    conn,
    *,
    config_path,
    run_bertrend_detection_fn,
    describe_signals_with_llm_fn,
    ask_fn,
    signal_model: str,
    embed_fn,
    parse_json_fn,
) -> tuple[list[dict], bool]:
    with conn.cursor() as cur:
        cur.execute("SELECT title FROM reports ORDER BY created_at DESC LIMIT 10")
        past_topics = [row[0] for row in cur.fetchall()]

    all_candidates = []

    try:
        signals = run_bertrend_detection_fn(conn, cfg_path=config_path)
        if signals:
            log.info(
                "BERTrend detected %d signals (%d weak, %d strong)",
                len(signals),
                sum(1 for signal in signals if signal["signal_class"] == "weak"),
                sum(1 for signal in signals if signal["signal_class"] == "strong"),
            )
            candidates = describe_signals_with_llm_fn(
                conn,
                signals,
                lambda system, user: ask_fn(system, user, model=signal_model),
                past_topics=past_topics,
            )
            if candidates:
                for candidate in candidates:
                    candidate["detection_method"] = "bertrend"
                log.info("BERTrend + LLM produced %d trend candidates", len(candidates))
                all_candidates.extend(candidates)
        else:
            log.info("BERTrend found no non-noise signals")
    except Exception as exc:
        log.warning("BERTrend detection failed (%s): %s", type(exc).__name__, exc, exc_info=True)

    try:
        pattern_candidates = detect_novel_tactical_patterns(conn, past_topics, embed_fn=embed_fn)
        if pattern_candidates:
            log.info("Tactical pattern detector found %d novel candidates", len(pattern_candidates))
            all_candidates.extend(pattern_candidates)
    except Exception as exc:
        log.warning("Tactical pattern detection failed (%s): %s", type(exc).__name__, exc, exc_info=True)

    if all_candidates:
        deduped = dedupe_candidates(all_candidates)
        log.info("After deduplication: %d unique candidates", len(deduped))
        return deduped, False

    log.info("Both detectors returned nothing, falling back to LLM-only detection")
    return detect_trends_llm_only(conn, past_topics, ask_fn=ask_fn, parse_json_fn=parse_json_fn)
