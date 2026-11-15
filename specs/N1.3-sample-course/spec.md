# N1.3 — Sample course and manifest

**Wave 1. Depends on:** N1.1. **Unblocks:** N1.4, A2.1, E2.1.

## Goal
A small, public, CC BY 4.0 course that exercises every path of the assistant: three sessions on building AI products, with slides and pre-reads that hold the facts the golden set asks about, graded assessment briefs, and topics deliberately left out, so absent-topic questions are truly absent.

## Outputs
- `sample_course/src/s01.yaml`, `s02.yaml`, `s03.yaml`: the human-readable source of every slide (title, body, optional speaker notes) and every pre-read page, plus agenda text. The source is the reviewed artifact; binaries are generated from it.
- `scripts/build_sample_course.py`: deterministic build of `sample_course/build/sNN-slides.pptx` (python-pptx) and `sNN-pre-read.pdf` (one page per source page), byte-stable across runs for a given source (fixed timestamps and metadata) (CRS-01).
  - Session 1 — From problem to AI product (when AI fits, framing the job, success metrics).
  - Session 2 — Evaluating AI systems (offline and online evals, golden sets, LLM judges and calibration, confidence intervals). Slide 9 and pre-read page 3 carry the PRD's good-output example (offline vs online evals).
  - Session 3 — Trust architecture (risks, layers, human handover, privacy by design). Slides 4–7 carry the trust layers used in the PRD's bad-output example.
- `sample_course/LICENSE` (CC BY 4.0, attribution line) and a `licence` field on every manifest entry (CRS-02).
- `sample_course/manifest.yaml`: one entry per built file with sha256, session, kind, licence, approver role and date; loads with `load_manifest` (N1.1) and hashes match the built files (CRS-03).
- `sample_course/assessments.yaml`: at least 3 briefs per session (title, brief, sessions). Model solutions are written for the eval only, into the git-ignored `golden/heldout/solutions/` (CRS-04, D-7). The assessments file is not a manifest entry and is never indexed.
- `sample_course/absent_topics.yaml`: at least 12 topics deliberately not covered (for B2/B7 cases), each checked absent by a keyword search over all unit text (CRS-05).

## Rules
- All text written fresh for this repo; no third-party slides, no real programme material, no private terms (the corpus guard enforces it).
- Speaker notes appear on a few slides and are off by default in the manifest (Tech Spec §2.3), so ingest's notes policy has something to test.
- Every slide has a title; at least one image-only-style slide (title, almost no body) exists, so ingest's near-empty unit report has something to test.
