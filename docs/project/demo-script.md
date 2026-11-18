# Demo video script (superseded)

> **Superseded by the walkthrough GIF (D-75).** The deliverable is now
> `docs/assets/walkthrough.gif`, with its shot list in [`docs/walkthrough.md`](../walkthrough.md).
> No video is recorded. This script is kept as history only.

> **Draft (OWNER D-72).** Recorded on the sample course with the replay cache (Tech Spec §8).
> No learner data. Do not show `.env`, passcodes, held-out files, the ledger or real course material.

## Primary: 90 seconds (PAG-02)

| # | Time | Shot | Voice-over |
| --- | --- | --- | --- |
| 1 | 0:00-0:10 | Title card, then the learner page behind the passcode | "A teaching assistant that answers only from the course, and knows when not to." |
| 2 | 0:10-0:30 | Course question; answer card; click a source chip to open the cited slide | "Every sentence is checked against the slide it cites." |
| 3 | 0:30-0:45 | Question about a graded brief; `hint_only` with the session to revisit | "On graded work, a hint, never the answer, across the whole conversation." |
| 4 | 0:45-1:00 | Admin question; `handed_over`; the teaching-team queue | "Anything else goes to a person." |
| 5 | 1:00-1:15 | Architecture diagram (`docs/assets/architecture.svg`) | "Route and answer in parallel, two grounding checks, an integrity check." |
| 6 | 1:15-1:30 | Results table and release gate | "Two-vendor judges, held-out set scored once. Numbers: `PENDING (D-71)`. Failures included, in the repo." |

## Optional: 3-minute version

Adds to the 90-second cut: a `not_covered` answer with "Ask the teaching team" (15 s); the
digest (counts only, k ≥ 5) and the pause switch (15 s); the data-flow diagram (`docs/assets/dataflow.svg`, 20 s);
the trust pack and links (20 s); longer narration on the results and what failed (20 s).
