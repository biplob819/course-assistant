# Privacy

**course-assistant has no telemetry.** It does not phone home, collect usage statistics or
send crash reports. This page describes how the software handles learner and course data
when you run it. It is not legal advice; the institution that runs it decides the legal
basis, the notice and the agreements.

Design source: Tech Spec §7 and NFR2 to NFR4, NFR7.

The trust pack maps each rule below to its DPDP Act and UK GDPR duty and to the test that
proves it ([`docs/trust/dpdp-mapping.md`](docs/trust/dpdp-mapping.md)), and gives the incident
steps ([`docs/trust/incident-runbook.md`](docs/trust/incident-runbook.md)).

## Roles

When a programme runs the assistant for its learners, the programme provider is the data
fiduciary (controller) and whoever operates the assistant is its processor, under a written
agreement. Before a programme signs off, no learner data exists at all: the demo uses random
session ids and the sample course.

## Where data goes

The software makes network requests only for these purposes:

| Purpose | Destination | When |
| --- | --- | --- |
| Answers, routing, checks and hints | The OpenAI project you configure (Responses API with `store=false`) | Each learner question |
| Course files and retrieval | A vector store in that same OpenAI project | `course-assistant ingest`, and each question |
| Evaluation judge | The same OpenAI account (gpt-6.1-sol); Anthropic as a second judge only when dual judging is on, which it is not since OWNER D-80 | Evaluation runs only, never at runtime; golden cases, never learner data |
| Traces (optional) | A Langfuse project you configure, if `trace_sink: langfuse` | Each question; no text |
| Demo only: daily spend counter | The hosted KV store you configure (one number per day) | Each live demo request |

Nothing is sent anywhere else. Learners are identified to providers only by pseudonymous ids;
no names or email addresses are sent. OpenAI may keep request data for abuse monitoring for
up to 30 days unless zero data retention applies to your project, and the first-use notice
says so.

## What is stored, and for how long

| Data | Where | Kept for | Deleted by |
| --- | --- | --- | --- |
| Conversations (question and answer text) | App store (SQLite) | Pilot length + 30 days, or until the learner deletes it | Learner's delete (one conversation or all), the purge job |
| Escalations and teaching-team replies | App store | As above | As above |
| Consent records | App store | Pilot length + 1 year (proof of consent) | The purge job (`Store.purge`) |
| Access log | App store | 1 year, no question text | The purge job (`Store.purge`) |
| Traces | JSONL file or Langfuse | **No question or answer text.** Target 30 days; removal by age is not automated yet | A learner's delete removes their lines at once. Older lines: delete or rotate `trace.jsonl`, or set retention in Langfuse |
| Weekly digest rows | App store | Pilot length; counts only | — |
| Course files and vector store | OpenAI | Until teardown | Delete the vector store and its files in your OpenAI project at the end of a pilot (a scripted teardown is planned, P4.3) |

The purge job (`Store.purge`) applies these periods and is built and tested. This version has no
command to run it yet: a `make purge` target is planned for v0.1.0 (R3.1), and running it on a
schedule is a pilot operations step (P4.2).

## Rules the code enforces

- **Consent by purpose.** The weekly digest counts only learners whose consent includes
  improving the course. Withdrawing consent stops new processing at once. Deletion is a
  separate action, available at any time.
- **Digest k-anonymity.** A topic with fewer than 5 askers in a week is dropped. Topic labels
  are written by a model from the questions and checked by a leak test, so a label cannot
  quote a question.
- **Deletion.** Deleting history removes rows from the app store at once and queues trace
  deletion. A deletion request is complete only when both are done, and the purge job flags any
  request still incomplete after 7 days.
- **FAQ entries** written after a material gap is flagged must not share an 8-word run with a
  stored question.
- **No email addresses.** A teaching-team reply appears in the learner's history with a
  "reply waiting" badge. The app does not hold an email address.
- **Access.** The teaching-team view sits behind its own passcode in the demo (single
  sign-on in a pilot), and every staff action is written to the access log.

## The hosted demo

The demo runs on the sample course only. Learner ids are random for each browser session,
the store resets each night, and logs are aggregate only. Passcodes are stored as salted
hashes. Streamlit usage statistics are off (`.streamlit/config.toml`). The hosting platform
keeps its own server logs under its own terms.

## On your own machine

- `runs/`: evaluation runs and the spend ledger (may hold model answers; git-ignored).
- `.evalkit/`: EvalKit's local cache (may hold held-out answers; git-ignored).
- `corpus_store/`: split course text from ingest (git-ignored).
- The app store and trace files under the app folder you configure (`CA_APP_DIR`).

Delete any of these at any time.
