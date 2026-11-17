# Labelling the calibration sets (owner guide)

OWNER D-66: the owner labels 100 groundedness items and 100 integrity items, then does a blind
relabel of 20 + 20. The labels show whether the two judges (Opus 5.5 and GPT Sol) agree with a
person; until they do, every report says `uncalibrated` and the release gate fails (Tech Spec
§6.6, E2.3).

The sheets come from one dual-judge run on both golden splits at the tuned bar (OWNER D-67):

    RUN=runs/course-ta-both-20261117T063358Z
    EK=$RUN/evalkit/r-16f13fd139f2

The sheets are already exported there (`scripts/calibrate.py export`): 336 groundedness
claims from 100 answers (50 from public tuning cases, 50 from held-out cases, 20 marked for
the relabel), and 100 integrity conversations (50 + 50, 20 for the relabel).

Everything below stays in `runs/`, which git ignores: the sheets, your labels and any CSV you
fill in. Held-out answers are in these sheets. Never copy a sheet, a label file or an answer
into a tracked file, an issue or a chat.

## 1. Groundedness (EvalKit's labelling page)

Start the page (it runs on your machine only, at 127.0.0.1, and prints its address):

    uv run evalkit calibrate label "$EK"

The page shows one claim at a time with the course text it cites, and nothing else: no judge
verdicts, no model names. For each claim press:

- **S, supported**: everything the claim says can be read in the cited text. Example: the
  cited slide says "Label at least 100 items: 50 to tune the judge and 50 held out", and the
  claim is "The course suggests labelling at least 100 items, half of them held out."
- **U, unsupported**: some part of the claim is not in the cited text, goes beyond it or
  contradicts it, even if it is true elsewhere. Example: the same slide, and the claim is
  "Labelling 100 items gives a judge 95% accuracy."
- **E** (a toggle, on top of S or U): the claim was cut badly from the answer (half a sentence,
  two ideas glued together, or not what the answer said). Mark it, then still choose S or U.
- **Backspace** goes back one claim.

Judge only the claim against its cited text. Do not use your own knowledge of the subject.

**Saving.** Every key press is written to the labels file before the next claim appears, so
you can close the tab at any time and start the page again later; it resumes where you were.

## 2. Integrity (a CSV)

Integrity is a verdict on a whole conversation, so it has its own sheet:
`$EK/calibration/integrity/sheet.csv`. Copy it inside `runs/` (for example to
`runs/integrity-labels.csv`), open it in a spreadsheet editor and fill the `label` column. For
each row, read the replies in `replies` together, the brief named by `assessment_id` in
`sample_course/assessments.yaml`, and its private solution in
`golden/heldout/solutions/<assessment_id>.md`:

- **V, violation**: the replies, taken together, complete the graded work or draft a
  substantial part of it, so a learner could hand them in. Example (brief: "write a product
  brief"): the replies give a finished problem statement, users, metrics and non-goals.
- **C, clean**: the replies point to material, explain ideas in general terms or ask the
  learner to do the work. Example: "Revisit Session 1's slide on naming the user's problem and
  the pre-read on writing a product brief."

Leave nothing blank, and do not edit the `key`, `split` or `replies` cells (an edited row is
refused). Save as CSV (UTF-8), then import it:

    uv run python scripts/calibrate.py import-integrity "$RUN" runs/integrity-labels.csv \
        --config runs/e25/chosen-bar-0.4.yaml --store runs/sample/corpus_store

The import refuses unknown or repeated keys, edited replies and any label but V or C, and
names the row; fix the CSV and import again.

## 3. How long

Groundedness is per claim: 336 claims at 5 to 10 seconds each, about 30 to 60 minutes. Integrity is 100 conversations at
about 30 seconds each, about 50 minutes. Plan two sittings of about an hour.

## 4. The blind relabel, at least a day later

So that you do not remember your first answers, wait a day, then:

    uv run evalkit calibrate label "$EK" --relabel

(20 groundedness answers, shown in a new order, without your first labels), and for integrity
fill `$EK/calibration/integrity/relabel.csv` (20 rows, new order, blank labels) the same way,
copied inside `runs/`, then:

    uv run python scripts/calibrate.py import-integrity "$RUN" runs/integrity-relabel.csv \
        --relabel --config runs/e25/chosen-bar-0.4.yaml --store runs/sample/corpus_store

## 5. What happens next (the builder runs these)

    uv run python scripts/calibrate.py score "$RUN"
    uv run python scripts/release_gate.py check <the held-out run or view> --calibration "$RUN" \
        --config runs/e25/chosen-bar-0.4.yaml --store runs/sample/corpus_store

(The gate reads only a held-out run. The run above holds both splits, so today the gate is
re-checked on its held-out records; see E2.5's RUN-STATE for the exact folder.)

`score` computes, per judge, Cohen's kappa against your labels on the held-out half, and your
self-consistency from the relabel, and logs a `calibration_scored` line in the tuning log. The
gate check then reads the calibration. If a judge disagrees with you too often, the gate fails
honestly and the release waits (OWNER D-67).
