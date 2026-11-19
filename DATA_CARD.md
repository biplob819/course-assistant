# Data card: the course-assistant golden set

This card gives the counts and composition of the golden set used to measure the assistant
(Tech Spec §6). It contains no held-out text. Held-out cases are private by design; the
repository holds only their ids and hashes.

## What it measures

Whether the assistant, on the sample course, answers course questions from the cited slide
or page, says "not covered" for anything the course does not contain, gives hints rather than
graded work, hands admin and personal requests to the teaching team, protects other learners'
data, never claims to be a person or to speak for the institution, and resists prompt
injection.

## Source material

The sample course in [`sample_course/`](sample_course/): three sessions (slides, pre-reads
and agendas), assessment briefs and a list of absent topics. It was written for this project
and contains no real programme material. Licence: CC BY 4.0
([`sample_course/LICENSE`](sample_course/LICENSE)).

## Authorship

- Cases were written before any prompt tuning, so they are not shaped by the system they
  measure.
- Each course question names its `gold_units`, the slide or page locators that support it
  (for example `s02:slides:9`). Builder-written cases were checked by a second reader (a fresh
  model verifier plus an owner spot-check).
- The adversarial extension (OWNER D-19, D-31) adds 3 paraphrases of each graded-work probe
  and each injection probe, written by a model outside the answer-model comparison.
  Multi-turn probes keep their turn structure.

## Size and composition

429 cases: 214 public (tuning) and 215 held-out. The plan had 430; held-out case
`ta-b1-065` was retired because its question had appeared in a public test (D-54).

| Rule | What a case checks | Expected status | Public | Held-out | Total |
| --- | --- | --- | --- | --- | --- |
| B1 course questions (30 follow-ups in the plan) | Answered from the cited unit | `answered` | 74 | 75 | 149 |
| B2 absent topics | Not answered from general knowledge | `not_covered` | 15 | 15 | 30 |
| B7 planted absent topics | True in general, absent from the course | `not_covered` | 5 | 5 | 10 |
| B3 graded-work probes, with paraphrases (30 + 90) | A hint, never the work | `hint_only` | 64 | 56 | 120 |
| B4 admin and personal | Handed to the teaching team | `handed_over` | 10 | 10 | 20 |
| B8 other learners | Never reveals another learner's data | `not_covered` | 5 | 5 | 10 |
| B9 identity | No false identity claim | `not_covered` or `answered` | 5 | 5 | 10 |
| B10 injection, with paraphrases (20 + 60) | Rules intact, canary never appears | as for the carrier question | 36 | 44 | 80 |
| **Total** | | | **214** | **215** | **429** |

Public split by expected status: 98 `answered`, 68 `hint_only`, 38 `not_covered`, 10
`handed_over`.

## Splits

- 50/50, stratified by rule and session, with a fixed seed.
- Split by group, not by case (D-32, D-34): every question about one unit, its follow-ups and
  the injections carried on it go to the same side; every probe for one assessment and its
  paraphrases go to the same side; cases sharing any question or earlier-turn text are merged.
  `python scripts/golden_tool.py check` refuses any held-out text found in the public split.
- Public cases: [`golden/public/cases.jsonl`](golden/public/cases.jsonl). Held-out:
  [`golden/heldout/MANIFEST.json`](golden/heldout/MANIFEST.json) only (ids, line hashes, file
  hash, corpus version and counts by rule).
- Prompts and thresholds are tuned on the public split only. A held-out set is scored once
  per verdict; the first held-out set has been scored (17 Nov 2026) and will be retired and
  replaced by a fresh set with the same composition rules (OWNER D-67, D-71).
- Assessment solutions for the graded-work probes sit beside the held-out data and are never
  indexed.

## Corpus drift

Every case carries the `corpus_version` it was written against. When a course change removes
a case's gold units, the case is retired, or becomes an absent-topic case if the topic is now
gone. Every session needs at least 15 course-question cases and every assessment at least 3
graded-work probes before it can go live.

## Known gaps

- **One small course.** Three sessions in one subject, in English. Results do not show how the
  assistant does on long courses, other subjects, other languages or other file layouts.
- **Written questions.** Cases were written by the builder, not collected from learners, so
  they are cleaner than real questions. D-69 adds cases from verifier findings, flagged
  escalations and digest topics, with owner approval.
- **Small zero-tolerance sets.** With 10 cases a rule (B8, B9), a perfect score shows only a
  failure rate under about 30% with 95% confidence (rule of three). The paraphrases raise B3
  and B10 to 120 and 80 cases.
- **Judging is calibrated against a model panel, not a person** (OWNER D-79). The calibration
  sets are labelled by gpt-6.1-terra and gpt-6-luna, with gpt-6.1-terra at high effort breaking
  a split, so kappa measures agreement with a model panel. The judge is one model, gpt-6.1-sol
  (OWNER D-80): no cross-family check, and the labellers are also the answer and check models,
  so they may share blind spots. A person's labels can replace the panel's later.

## Licence

The public golden cases, like the sample course they are written against, are released under
CC BY 4.0 (Tech Spec §8).
