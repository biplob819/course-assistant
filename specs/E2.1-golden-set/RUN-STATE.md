# E2.1 run state

**Now:** building

## History
- 2026-11-15 — spec written; D-30 (locator gold units) and D-31 (extension sizing) recorded.
- 2026-11-15 — built: authoring source and held-out cases in git-ignored golden/heldout/; tool builds, splits, checks and verifies; D-30, D-31, D-32 recorded.
- 2026-11-15 — second reader (verifier pass 1): GLD-01, 03 (152 B1 locators, 0 wrong), 04, 05, 06 confirmed; disproved GLD-02 on leakage (held-out questions copied into public follow-ups and B10 carriers; paraphrase-level overlaps; near-identical B3 pairs across splits). Fixed (D-34): split by merged topic and assessment groups with a cross-split text check in golden_tool; B9 reworded to pure identity, two paraphrases name their tasks, ta-b10-020 names its assignment; golden/README.md added.
- 2026-11-15 — second reader pass 2 confirmed GLD-01, 03-06; disproved GLD-02 on meaning-level leaks (prior-turn answers citing held-out units, shared secondary units, B2/B4/B9 near-paraphrases). Fixed (D-35): merge on all units, cited units, concept links, absent topics and themes; leak check compares units; GLD-02 wording and spec aligned.
- 2026-11-15 — second reader pass 3 confirmed GLD-01, 03-06; disproved GLD-02 (an assignment slide's question answered by public probes of that assessment; one B9 theme mis-tagged). Fixed: assignment slides joined to their session's first assessment; six more concept links; B8 themes and the stored-questions injection joined with them; B9 re-tagged; greedy size-balanced assignment within strata (217 public, 213 held out; B1 77/73). Per-session counts stay uneven (s02 15/35) because session 2's linked concepts form one group; the coverage rule holds overall.
- 2026-11-15 — second reader pass 4 confirmed GLD-01, 03-06; disproved GLD-02: the two reveal-your-prompt attacks without a course question had no shared group (ta-b10-015/063-065 vs public ta-b10-011/052/053), and the s01 brief pre-read page was not linked to assess:s01-a1 (ta-b1-037). Fixed: B10 theme 'b10:reveal-prompt' for both; s01:pre_read:3 joined to assess:s01-a1. Partial overlaps noted as should-fix (GLD-02 counts only a full answer readable from public). 215 public.
