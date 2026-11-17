# course-assistant: a grounded teaching assistant with an honest release gate

A teaching assistant for a course: learners ask questions and get answers that cite the slide or
page they come from. It hands personal and admin questions to the teaching team, and gives hints,
never solutions, on graded work. The hard part was proving it behaves, not building the chat.
Every release goes through a gate built on EvalKit. The gate scores 429 golden cases, split
into a public half for tuning and a private held-out half scored once, using judges from two
vendors. It enforces zero tolerance on integrity, leaks, injection and identity, and checks
latency and cost.

The first held-out run failed: grounded 0.693 against a 0.90 target. A tuning round on the
public half alone raised grounding to 0.919 there. On a fresh held-out set it reached 0.813, with
locator accuracy 0.874 and two identity failures. The gate still exits 1, and the README
publishes that verdict. Integrity went from 0.929 to 56 of 56. Latency is 10.0 s at p95, and
it costs $0.0075 a question ([report](measurements/e2.5-report.md)).

## My role

I wrote the product requirements and owned every ruling that shaped the system: multi-turn
conversations, a passcode on every page, the monthly spend cap, the order in which held-out data
may be used, and the restated latency target. I approved each paid evaluation run and do the
human calibration labels and the live deploy myself.

Claude Code built the code, tests, evaluation runs and documents under EvalKit's node harness.
Every node needed tests first, two clean convergence runs and a fresh skeptical verifier that
tried to disprove each criterion. Nodes went green only when nothing was left to disprove. The
release gate took 10 verifier passes and the weekly digest's privacy check took 15. Every scope
limit is written down as a decision ([decisions](decisions.md)).

## Key decisions

**Score the held-out set once, and only after tuning (D-62, D-67, D-71).** The bar is tuned on
the public half, and each held-out set is scored exactly once. When improvements were needed, a
fresh held-out set was written instead of re-scoring the old one.
*Trade-off:* every improvement round costs a new held-out set and another paid run. In exchange,
the published held-out number is one nobody tuned against.

**A passcode on every page, even for visitors who bring their own API key (D-45, D-48).** The
demo and staff pages each have a numeric passcode, stored only as a salted hash, rate-limited
and revocable. Learner and staff codes can never match.
*Trade-off:* friction for casual visitors to the open-source demo. In exchange, nobody can drain
the spend cap or reach the staff queue anonymously.

**Keep the stronger answer model and restate the latency target to 11 s (D-61, D-65).** No
setting met the original 8 s p95. The faster model answered 30 of 74 course questions where the
stronger one answered 51.
*Trade-off:* answers take about two seconds longer at p95. In exchange, the answers are more
often grounded and less often "not covered".
