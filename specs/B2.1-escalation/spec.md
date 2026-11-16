# B2.1 — Escalation queue

**Wave 2. Depends on:** N1.5, A2.5. **Unblocks:** B2.3, B2.4, B2.5, D3.1.

## Goal
A question the assistant could not or should not answer reaches the teaching team with its
reason, the assistant's draft and the sources it used; an instructor replies, and the reply
waits in the learner's history marked as from the teaching team (US5, FR8, Flow B, finding
H5). An instructor can flag a material gap, and every escalation carries the timestamps the
reply-time metric needs (US5, US6, finding E8).

## Outputs

### `src/course_assistant/escalation/` (`queue.py`, `_errors.py`)

#### Escalate (ESC-01)
- `escalate_turn(turn, *, store, reason=None, at=None) -> Escalation` takes A2.5's `Turn`,
  so the kept not-covered draft (never stored on the Answer) is bound to its own question.
  The reason defaults from the turn: `handed_over` → the route (`admin` or `personal`);
  a `not_covered` turn showing the `NOT_COVERED` text → `not_covered`; `hint_only` with
  route `graded_work` → `graded_work`; anything else → `learner_request`.
- `queue_handover(turn, *, store, at=None) -> Escalation | None` queues a `handed_over`
  turn at once ("Sent to the teaching team" plus the queue entry, §2.2) and returns None for
  every other turn. The chat page (B2.5) calls it after every turn.
- `escalate(question_id, reason, *, learner_pid, store, at=None) -> Escalation` is the later
  "Ask the teaching team" tap on a stored turn, with no kept draft in hand (the draft is the
  shown answer where one was shown).
- Eligibility, checked against the stored question and answer (the question must be this
  learner's):

| Reason | Stored answer |
| --- | --- |
| `not_covered` | status `not_covered` with the fixed `NOT_COVERED` text |
| `graded_work` | status `hint_only` |
| `admin`, `personal` | status `handed_over` |
| `learner_request` | `answered`, `hint_only`, `error`, `handed_over`, or `not_covered` with `NOT_COVERED` |

  Off-topic and injection replies (no handover offer, §2.2) and paused turns (nothing
  stored) are refused. A question with no answer yet is refused. `admin` and `personal`
  need the route, which only the turn carries, so `escalate` refuses them; a stored
  handover that `queue_handover` missed can still be raised as `learner_request`.
- Draft and sources: a not-covered turn's kept draft (text, citations, hits); for a shown
  answer or hint, that text with its citations and hits; none for `admin`, `personal` and
  `error` (the answer call was discarded or failed). A draft the leak scan flagged is never
  attached (it may hold the canary, a pid or rules text).
- An escalation already open for the question is returned unchanged (a double tap never
  overwrites a reply). Status `open`, `created_at` = `at` (never before the question was
  asked).

#### Reply (ESC-02)
- `reply(question_id, text, *, instructor, store, at=None) -> Escalation`: `instructor` is a
  role id (D-28), `text` is non-blank and at most `MAX_REPLY_CHARS` (4,000). Allowed from
  `open` and `material_gap` (a flagged gap keeps its status and gains the reply); a second
  reply, or a reply to a closed escalation, is refused. `replied_at` = `at`, never before
  `created_at`. The change and its `reply` audit event are one transaction (N1.5's
  `Store.update_escalation`, a compare-and-set added here), so two instructors replying at
  once cannot overwrite each other.
- `learner_replies(pid, store) -> tuple[LearnerReply, ...]`: the learner's replies, newest
  first, each with `label = TEAM_LABEL` ("From the teaching team"), the role id and the time.

#### Statuses and timestamps (ESC-04)
- `flag_gap(question_id, *, instructor, store, at=None)`: `open` or `replied` →
  `material_gap` (a reply stays), audit `flag_gap`. `close(...)`: any status but `closed` →
  `closed` (a reply stays), audit `close`. Every other transition is refused.
- `team_queue(store, *, viewer, status=None, at=None) -> tuple[QueueItem, ...]`: the queue
  for the teaching team, oldest first: question id, question text, session, reason, status,
  draft and sources, reply and times. Audited `view_queue` (no target, no text).
- `reply_times(store) -> ReplyTimes`: count of replied escalations and the median and p90
  seconds from `created_at` to `replied_at` (the pilot reply-time metric). Numbers only.

#### Reply waiting in history (ESC-03)
- B2.2's `history` already shows `handover` (the status) and `reply` for each turn; B2.1
  writes exactly the fields it reads. `reply_waiting(pid, store) -> int` counts the
  learner's turns with a reply (the "reply waiting" badge). No email address, no outside
  notification (§7, H5).

### A2.5 addition
- `Turn.route` (the route category, None when paused or the route call failed), so the
  queue can tell `admin` from `personal`. Additive: nothing A2.5 decides changes.

## Rules
- Teaching-team actions (`view_queue`, `reply`, `flag_gap`, `close`) are audited with the
  role id, the action and the question id only: never question, draft or reply text (NFR7).
- Nothing in this node writes to a trace (D-14): no learner text, no reply text.
- Errors are one public `EscalationError` with fixed text (`invalid`, `refused`,
  `not_found`, `failed`) raised outside any except block (L-30), so a reply or question
  never reaches a caller through a message, cause or context.
- Escalations are learner text: they live in the app store, are removed by B2.2's delete
  (whole history or one conversation) and by `purge` under §7 retention.
- No network or API call; `golden/heldout/` is never read.
