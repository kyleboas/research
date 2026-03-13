import unittest

from autoresearch_report.evaluator import evaluate_items, score_report


GOOD_REPORT = """# Example

## Executive Summary
This report finds a strong tactical pattern supported by multiple sources. [S1:C10]

---

## Key Findings
1. The pattern appears repeatedly across matches and contexts. [S1:C10] [S2:C20]

---

## Main Analysis
The mechanism is visible in build-up shape, spacing, and timing. [S1:C10] [S2:C20] [S3:C30]

---

## Counterevidence and Alternative Explanations
Some sources suggest the pattern is matchup-dependent rather than broadly stable, which weakens the strongest version of the claim. [S4:C40]

---

## Evidence Assessment
The evidence is reasonably diverse but still concentrated in analyst reports rather than event data. [S1:C10] [S2:C20]

---

## Implications
Teams may respond by changing rest-defense shape and first-line pressure triggers. [S3:C30]

---

## Open Questions
- How persistent is the pattern over a longer time horizon? [S4:C40]

---

## Sources
- Source 1 https://example.com/1
- Source 2 https://example.com/2
- Source 3 https://example.com/3
- Source 4 https://example.com/4
"""


WEAK_REPORT = """# Thin report

## Executive Summary
This might be interesting.

## Sources
- Source only
"""


class ReportEvaluatorTests(unittest.TestCase):
    def test_score_report_rewards_structure_and_citations(self):
        scored = score_report(
            {
                "title": "Good",
                "content": GOOD_REPORT,
                "citation_validation": {"citation_count": 10, "invalid_citation_count": 0},
            }
        )

        self.assertGreater(scored["final_score"], 70)
        self.assertEqual(scored["invalid_citation_count"], 0)
        self.assertGreaterEqual(scored["section_coverage"], 0.99)
        self.assertGreater(scored["citation_validity"], 0.99)

    def test_evaluate_items_ranks_stronger_report_first(self):
        result = evaluate_items(
            [
                {
                    "id": 1,
                    "title": "Weak",
                    "content": WEAK_REPORT,
                    "citation_validation": {"citation_count": 0, "invalid_citation_count": 0},
                },
                {
                    "id": 2,
                    "title": "Good",
                    "content": GOOD_REPORT,
                    "citation_validation": {"citation_count": 10, "invalid_citation_count": 0},
                },
            ]
        )

        self.assertEqual(result["ranked"][0]["id"], 2)
        self.assertGreater(result["metrics"]["final_score"], 0)


if __name__ == "__main__":
    unittest.main()
