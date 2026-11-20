# Pilot pack: running course-assistant with one real cohort

This is what a pilot needs, in order, and who does each part. It covers the three pilot nodes
of the build graph: P4.1 onboarding, P4.2 operations (four weeks) and P4.3 report and teardown
(Tech Spec §11, `docs/graph/graph.md`). The public demo is not a pilot: it runs on a sample
course with passcodes and resets its data every night.

Nothing here makes a model call. Costs are measured figures from the shipped setting (D-90).

## 1. Costs

| Item | Cost | Source |
| --- | --- | --- |
| Learner questions | $0.0061 a question: about $6 for 1,000 | heldout-4 run, `docs/measurements/e2.5-heldout4-report.md` |
| Held-out eval runs (weekly gate, manifest changes) | about $3.44 a run: about $21 for 6 | same run |
| A pilot month (1,000 questions, 6 runs) | **about $27**, against the $150 cap (alarm at $135) | Tech Spec §9, OWNER D-20, D-64 |
| Ingesting the cohort's course | no model call; OpenAI charges vector-store storage while the store exists (see its file search pricing) | OpenAI pricing |
| Approving the first course version | one held-out eval run, about $3.50 | `course-assistant eval` |
| Teaching-team calibration relabel | no API cost (people label; scoring is offline) | `docs/labelling.md` |

All of it bills to the OpenAI project you configure. Set a hard monthly limit on that project.

## 2. What exists and what must be built first

Built and tested (v0.1.0): the answer path, both pages, escalation and replies, the weekly
digest, privacy notice and consent by purpose, history deletion, the purge job (`make purge`),
pause switches, the release gate and `course-assistant eval`, and serving only the
gate-approved course version (`LIVE.json`).

Built for the pilot after v0.1.0 (tests in `tests/pilot/`, no model call):

| Piece | How to use it | Where |
| --- | --- | --- |
| Learner ids from platform ids, `HMAC-SHA256(platform_user_id, PID_SECRET)` cut to 16 hex | `course_assistant.privacy.pseudonym.learner_pid(platform_id, secret)`; refuses a missing id or a short secret, and its errors never carry either | D-13, `src/course_assistant/privacy/pseudonym.py` |
| A persistent app store | Set `CA_DAILY_RESET=0` on the pilot host: one `app.sqlite` in `CA_APP_DIR`, created on first use, never reset (the demo's `1` keeps one store a day) | `src/course_assistant/app/wiring.py` |
| Teardown with a proof | `make teardown CONFIG=... APP_DIR=... PROOF=... YES=--yes` (or `course-assistant teardown`): deletes every file in the vector store and the store, checks it is gone, deletes the app store and traces, writes a proof (ids and counts only); exit 1 if anything is left | P4.3, `src/course_assistant/pilot/teardown.py` |

Still to build, after your choices in section 3:

| Gap | Why it matters | Where it is specified |
| --- | --- | --- |
| Sign-in through the learning platform (SSO) wired into both pages | The pages use passcodes today; with SSO each learner's page uses `learner_pid` of their platform id, so history, consent and deletion follow the person | D-13, PIL-03 |
| Langfuse trace sink, if chosen | Only the local JSONL sink exists; `trace_sink: langfuse` is a config value with no sink behind it | D-15 |

## 3. Decisions and documents from you (P4.1)

1. **Pilot host.** Where the pages run for the cohort. It needs: Python 3.12, a persistent disk
   for the SQLite app store, secrets set as environment variables, HTTPS, and sign-in in front
   of the pages. Options: a private deploy under the programme provider's account; a small
   container host with a persistent volume behind the provider's sign-in. The public Streamlit
   Community Cloud app is not suitable (public, no persistent disk).
2. **Sign-in.** The platform's single sign-on (OIDC or SAML) and which claim carries the
   learner's platform id. Staff sign in the same way, with a staff role.
3. **Processor agreement.** The programme provider is the data fiduciary; you are its
   processor (Tech Spec §7). Points it should cover: purposes (answering questions; optional
   course improvement counts), data held (questions, answers, escalations, pseudonymous ids,
   consent records, access log), retention (pilot length + 30 days; consent proof 1 year;
   access log 1 year), sub-processor (OpenAI, `store=false`; its abuse-monitoring retention),
   deletion on request within 7 days, breach notice (the 72-hour runbook,
   `docs/trust/incident-runbook.md`), and teardown at the end. The duty-by-duty map is
   `docs/trust/dpdp-mapping.md` (DPDP with a UK GDPR column).
4. **Tracing.** Local JSONL files only, or Langfuse (and its region). JSONL needs no new
   processor; Langfuse does.
5. **Notice text.** The notice the learner page shows (`privacy/notice.py`), with the
   provider's name and contact added, and the languages it is needed in.
6. **Budget.** The monthly cap on the pilot's OpenAI project (the measured month is about $27).
7. **Pilot dates.** Start and end, so the purge job knows the pilot end.

## 4. Onboarding steps (P4.1)

| # | Step | Who | Command or document |
| --- | --- | --- | --- |
| 1 | A folder for the cohort: config, manifest, assessments | builder | `course-assistant init <folder>` |
| 2 | Approved course files listed in the manifest (private, never in this repo) | teaching team | `manifest.yaml` |
| 3 | Assessment briefs (no model solutions in any prompt; solutions kept apart) | teaching team | `assessments.yaml` |
| 4 | Checks before any call | builder | `course-assistant doctor`, `course-assistant ingest --dry-run` |
| 5 | Ingest into a vector store in the pilot's OpenAI project | builder | `course-assistant ingest` |
| 6 | 20 to 30 real questions from the teaching team, split into tuning and held-out cases | teaching team, builder | `docs/guide/evaluate.md` |
| 7 | Teaching-team relabel of the calibration samples (replaces the model panel's labels) | teaching team | `course-assistant calibrate`, `docs/labelling.md` |
| 8 | A held-out eval that passes the gate approves the course version the pages serve | builder | `course-assistant eval` |
| 9 | Deploy on the pilot host with SSO; notice and consent live; pause switches tested | builder, owner | `docs/deploy.md` (adapted to the host) |
| 10 | Owner attests P4.1 (`OWNER D-n` opening "Attests P4.1") | owner | `docs/decisions.md` |

## 5. Running the pilot (P4.2, four weeks)

- Weekly: the release gate on the held-out cases (about $3.44), the digest to the teaching
  team, `make purge PILOT_END=YYYY-MM-DD YES=--yes` daily or at least weekly.
- Teaching team: answer the queue, flag material gaps, pause a session if needed
  (`docs/trust/incident-runbook.md`).
- One incident drill: pause, notify, the 72-hour report path, resume.
- Metrics: questions, statuses, handover share, p95 latency and spend (the Ops tile).

## 6. Closing the pilot (P4.3)

A pilot report to the programme owners (usage, results, incidents, costs), then teardown:
delete the vector store and its files in the pilot's OpenAI project, delete the app store and
traces, and keep the proof (`make teardown`; see section 2).

## 7. Teaching-team quick guide

- **Queue.** Questions the assistant could not answer or sent to you. Reply in the page; the
  learner sees it in their history. Mark a question a material gap if the course should cover
  it.
- **Pause switches.** Pause one session or all; learners then get a fixed message and no
  model call is made. Every change is logged.
- **Digest.** Weekly topics asked by at least five consenting learners; counts only, no
  question text.
- **Materials.** Which course files the assistant serves and which version.
