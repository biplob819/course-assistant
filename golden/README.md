# Golden set

430 cases that measure the course assistant on the sample course (Tech Spec §6.1).

| Rule | Cases | What a case checks |
| --- | --- | --- |
| B1 course questions | 150 (30 follow-ups) | answered from the cited slide or page |
| B2 absent topics | 30 | "not covered", no answer from general knowledge |
| B7 planted | 10 | true in general, absent from the course: must be "not covered" |
| B3 graded work | 30 + 90 paraphrases | a hint and a session to revisit, never the work itself |
| B4 admin and personal | 20 | handed to the teaching team |
| B8 other learners | 10 | never reveals another learner's data |
| B9 identity | 10 | never claims to be a person or to speak for the institution |
| B10 injection | 20 + 60 paraphrases | the rules stay intact; the canary never appears |

## Files

- `public/cases.jsonl`: the tuning split. Use it to develop prompts and thresholds.
- `heldout/MANIFEST.json`: the ids and line hashes of the held-out split, so anyone can check
  that the private file has not changed. The held-out cases themselves stay private and are
  never committed, so reported results are not training accuracy (Tech Spec D-5).

Each line is a `GoldenCase` (`src/course_assistant/schemas/config.py`). `gold_units` name the
supporting slide or page as a locator: `s02:slides:9` is slide 9 of Session 2. A cited unit is
one place the answer lives; the eval also accepts other units judged to support it (§6.4).

## Splitting

Cases are split by group, not one by one (D-32, D-34): every question about one slide, its
follow-ups and the injections carried on it go to the same side; all probes for one
assessment and their paraphrases go to the same side; and cases that share any question or
earlier-turn text are merged. `python scripts/golden_tool.py check` refuses any held-out
question or earlier turn found in the public split.
