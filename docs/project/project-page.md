# course-assistant: a teaching assistant that knows when not to answer

> **Draft (OWNER D-72).** Laid out under the nine architecture areas (PRD; PAG-01). Numbers
> that depend on the final run are `PENDING (D-71)`. Demo: `DEMO_URL pending owner deploy`.

## The problem

Learners ask the same questions between sessions, often when no one is around. A general
chatbot will answer, but it also invents facts, hands back graded work and speaks with an
authority it does not have. This assistant answers only from the course's own materials, cites
the slide, gives hints rather than solutions on graded work, and passes everything else to a
person (`README.md`, Who it is for).

## Results (honest, current)

The first full run (17 Nov 2026, 429 cases, judges uncalibrated) **failed the gate**: held-out
grounded 0.693 and locator 0.800 against 0.90 and 0.95, integrity 52/56 against zero
tolerance. Citation validity, handover, injection, leak, identity and latency passed
(`docs/measurements/e2.5-report.md`). An improvement round on the public half and one score on a
fresh held-out set follow (OWNER D-71).

| Headline (with CI and stamp) | Release value |
| --- | --- |
| Grounded answer rate | `PENDING (D-71)` |
| Locator correct | `PENDING (D-71)` |
| Integrity | `PENDING (D-71)` |
| Latency p95 | `PENDING (D-71)` |
| Cost per question | `PENDING (D-71)` (first run $0.0062) |
| Calibration | `PENDING (D-71)` |

## 1. Models

Sol `medium` writes answers (51 vs 30 of 74 public B1 answered, against Luna); Luna routes,
checks and writes hints; Claude Opus 5.5 and GPT Sol judge offline (D-12, D-61, OWNER D-65).
[Model card](../trust/model-card.md).

## 2. Applications

Each question runs through a small typed graph: route, retrieve and answer, check, finalize
(D-1). Router and answer start in parallel (D-9). Four statuses: `answered`, `not_covered`,
`hint_only`, `handed_over` ([behavioral spec](../trust/behavioral-spec.md)). Two pages: learner
chat and teaching-team view (Tech Spec §8).

![Architecture](../assets/architecture.svg)

## 3. Data

One file per slide or page, so a file citation is a slide citation (D-2); our own copy of every
unit's text for source chips and checks (D-3). Sample course written for the demo, under
CC BY 4.0 (`DATA_CARD.md`).

## 4. Integration

Responses API with hosted file search (PRD); EvalKit for evaluation, with reusable parts
promoted there (OWNER D-68); teaching-team queue for hand-overs (`specs/B2.1-escalation/`).

![Data flow](../assets/dataflow.svg)

## 5. Security

Learner and retrieved text go in as data in delimiters; a leak scan on every reply with a
written scope (D-51); passkey gate on every route (OWNER D-45, D-48); spend guard with one
cost number. [Threat model](../trust/threat-model.md).

## 6. Privacy

Pseudonymous ids, `store=false`, traces with no text, deletion with a purge deadline, digest
counts only with k ≥ 5 (`PRIVACY.md`). [DPDP mapping](../trust/dpdp-mapping.md), gaps marked.

## 7. Governance

Every choice is an append-only decision (`docs/decisions.md`); owner rulings are marked; each
node passes a fresh-context verifier before green (`specs/*/RUN-STATE.md`). Pause switches and
passkey revocation ([runbook](../trust/incident-runbook.md)). [Known limits](../trust/known-limits.md).

## 8. Evaluation

Golden set written before tuning; tuning on the public half only; held-out scored once;
dual-vendor judges; owner calibration labels; release gate with paired Holm regression; a
hash-chained tuning log. [Methodology](../trust/evaluation-methodology.md).

**What was hard:** the integrity check must read the whole conversation, since a brief can be
built up piece by piece (D-8). The leak scan's promise had to be written down after many
verifier passes (D-51). Latency with two post-answer checks was cut by early finalize and hints
on a smaller model, with the target restated to 11 s by owner ruling (D-61, OWNER D-65).

## 9. Deployment

Streamlit demo on the sample course, opening on recorded answers with no live call; live
questions behind passcodes, per-request and daily caps, and a separate provider project with a
hard limit (Tech Spec §8; `docs/deploy.md`). Status: pending owner deploy (D-60).

## Demo and links

`[Video placeholder: 90 seconds, see demo-script.md]` · `DEMO_URL pending owner deploy`

Source: github.com/biplob819/course-assistant · EvalKit: github.com/biplob819/evalkit ·
Tech Spec, PRD and decisions in the repo (`README.md`, Links).
