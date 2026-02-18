---
name: prd
description: Generate a PRD in /tasks/prd-<feature-name>.md by first asking 3–5 essential clarifying questions with A/B/C options, then writing a junior-dev-friendly PRD in Markdown.
disable-model-invocation: true
---

You are in "PRD mode". Follow this exact process:

PHASE 1 — Clarify (do not write the PRD yet)
1) Ask 3–5 clarifying questions ONLY (no extra commentary).
2) Number questions (1, 2, 3...) and provide A/B/C/D options for each.
3) Questions must focus on: problem/goal, core functionality, scope boundaries, success criteria.
4) End PHASE 1 by asking the user to reply with selections like "1A, 2C, 3B".
5) STOP after the questions and wait for the user's answers.

PHASE 2 — Write PRD (only after user answers)
1) Infer a feature slug in lowercase-kebab-case from the prompt (or use a short slug if provided).
2) Create /tasks/prd-<slug>.md (make /tasks if missing).
3) Write a PRD in Markdown with these sections:
   1. Introduction/Overview
   2. Goals (specific + measurable)
   3. User Stories
   4. Functional Requirements (numbered “The system must…” statements)
   5. Non-Goals (Out of Scope)
   6. Design Considerations (optional)
   7. Technical Considerations (optional)
   8. Success Metrics
   9. Open Questions
4) Assume the reader is a junior developer: explicit, unambiguous, minimal jargon.
5) Do NOT implement anything. Do NOT modify other files beyond creating the PRD.
6) After writing the file, respond with:
   - The path you wrote
   - A 1–2 line summary of what’s in it