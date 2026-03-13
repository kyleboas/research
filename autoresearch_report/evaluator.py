from __future__ import annotations

import json
import re
from pathlib import Path

REQUIRED_SECTIONS = [
    "Executive Summary",
    "Key Findings",
    "Main Analysis",
    "Counterevidence and Alternative Explanations",
    "Evidence Assessment",
    "Implications",
    "Open Questions",
    "Sources",
]

CITATION_RE = re.compile(r"\[S(\d+):C(\d+)\]")
H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def load_fixture(path: str | Path):
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, list):
        raise ValueError("fixture must be a JSON array")
    return payload


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def _extract_citations(text: str) -> list[tuple[int, int]]:
    return [(int(source_id), int(chunk_id)) for source_id, chunk_id in CITATION_RE.findall(text or "")]


def _headings_present(text: str) -> set[str]:
    return {heading.strip().lower() for heading in H2_RE.findall(text or "")}


def _extract_section_body(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n?(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text or "")
    return (match.group(1) if match else "").strip()


def _count_listed_sources(text: str) -> int:
    body = _extract_section_body(text, "Sources")
    if not body:
        return 0
    count = 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("-", "*")) or re.match(r"^\d+\.\s+", stripped) or "http" in stripped:
            count += 1
    return count


def _normalize_expected(value):
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"publishable", "strong", "pass", "good"}:
        return "publishable"
    if normalized in {"needs_work", "weak", "fail", "bad"}:
        return "needs_work"
    return None


def score_report(item: dict) -> dict:
    content = str(item.get("content") or "")
    citations = _extract_citations(content)
    unique_sources = sorted({source_id for source_id, _chunk_id in citations})
    headings = _headings_present(content)
    sections_present = sum(1 for section in REQUIRED_SECTIONS if section.lower() in headings)
    section_coverage = sections_present / len(REQUIRED_SECTIONS)
    words = _word_count(content)
    listed_sources = _count_listed_sources(content)

    validation = item.get("citation_validation") or {}
    citation_count = int(validation.get("citation_count", len(citations)) or 0)
    invalid_citation_count = int(validation.get("invalid_citation_count", 0) or 0)
    valid_citation_count = max(0, citation_count - invalid_citation_count)

    citation_validity = (valid_citation_count / citation_count) if citation_count else 0.0
    target_citations = max(6, words / 180) if words else 6
    citation_density = min(1.0, citation_count / target_citations)
    source_diversity = min(1.0, len(unique_sources) / 6) if unique_sources else 0.0
    sources_section_coverage = min(1.0, listed_sources / max(1, len(unique_sources))) if listed_sources else 0.0
    thoroughness = min(1.0, words / 2200) if words else 0.0

    counter_body = _extract_section_body(content, "Counterevidence and Alternative Explanations")
    counterevidence_coverage = 1.0 if _word_count(counter_body) >= 60 else 0.0

    final_score = round(
        (
            0.22 * section_coverage
            + 0.23 * citation_validity
            + 0.15 * citation_density
            + 0.12 * source_diversity
            + 0.10 * sources_section_coverage
            + 0.08 * counterevidence_coverage
            + 0.10 * thoroughness
        )
        * 100,
        2,
    )

    return {
        **item,
        "expected": _normalize_expected(item.get("expected")),
        "word_count": words,
        "citation_count": citation_count,
        "invalid_citation_count": invalid_citation_count,
        "unique_source_count": len(unique_sources),
        "listed_source_count": listed_sources,
        "section_coverage": section_coverage,
        "citation_validity": citation_validity,
        "citation_density": citation_density,
        "source_diversity_score": source_diversity,
        "sources_section_coverage": sources_section_coverage,
        "counterevidence_coverage": counterevidence_coverage,
        "thoroughness": thoroughness,
        "final_score": final_score,
    }


def evaluate_items(items):
    scored = [score_report(item) for item in items]
    ranked = sorted(scored, key=lambda item: (-item["final_score"], -item["citation_validity"], str(item.get("title", "")).lower()))

    labeled = [item for item in ranked if item["expected"] in {"publishable", "needs_work"}]
    publishable_hits = [item for item in labeled if item["expected"] == "publishable"]
    needs_work_hits = [item for item in labeled if item["expected"] == "needs_work"]

    avg = lambda key: (sum(float(item.get(key, 0.0) or 0.0) for item in ranked) / len(ranked)) if ranked else 0.0

    metrics = {
        "average_item_score": avg("final_score"),
        "section_coverage": avg("section_coverage"),
        "citation_validity": avg("citation_validity"),
        "citation_density": avg("citation_density"),
        "source_diversity": avg("source_diversity_score"),
        "sources_section_coverage": avg("sources_section_coverage"),
        "counterevidence_coverage": avg("counterevidence_coverage"),
        "thoroughness": avg("thoroughness"),
        "total_reports": len(ranked),
        "labeled_reports": len(labeled),
        "publishable_labels": len(publishable_hits),
        "needs_work_labels": len(needs_work_hits),
        "final_score": round(avg("final_score"), 2),
    }
    return {"ranked": ranked, "metrics": metrics}
