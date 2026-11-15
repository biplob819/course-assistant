# E2.1 acceptance criteria

**GLD-01** 280 cases in the §6.1 composition (150 B1, 40 B2/B7 with 10 planted, 30 B3, 20 B4, 10 B8, 10 B9, 20 B10), each a valid `GoldenCase`; every B1 case names locators that exist in the sample course; every B3 case names an existing assessment; B8 and B10 cases carry canaries.
**GLD-02** The 280 are split 50/50, stratified by rule and session, with a fixed seed; held-out cases live only in the git-ignored file; `golden/heldout/MANIFEST.json` lists their ids, line hashes, counts and corpus version, and matches the private file.
**GLD-03** A fresh reader confirms every B1 locator contains its answer and every expected status matches its rule; disagreements are fixed or ruled in decisions.md.
**GLD-04** Coverage: at least 15 B1 cases per session and at least 3 B3 probes per assessment; B2/B7 questions match no unit text.
**GLD-05** The extension holds 150 cases: 3 paraphrases of each B3 (90) and B10 (60) probe, each in its source's split (D-32), each keeping the source's expected status, assessment and canary, and multi-turn structure where the source has it.
**GLD-06** Multi-turn cases: 30 B1 follow-ups, 10 B3 decompositions and 5 B10 window injections carry well-formed `prior_turns`.
