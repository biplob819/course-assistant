# A2.3 — Grounding check

**Wave 2. Depends on:** A2.2. **Unblocks:** A2.5.

## Goal
Decide whether an answer draft may be shown as `answered`: every claim must come from the
cohort's own materials (FR6, B1, B7). A retrieval score measures relevance, not support
(finding C1), so grounding has two layers (D-4): a deterministic check of citations and score,
then a sentence-level support check on the check model against the cited units' text only.

## Outputs (`src/course_assistant/answer/ground.py`)
- `GroundStep(config, corpus, responder)`; `run(draft, marks, session=None) -> GroundingResult`
  and `assess(...) -> GroundReport` (the result plus sentences, support-call count, cost and
  latency, so Finalize can record spend). `draft` is A2.2's `AnswerDraft`; `marks` are the raw
  `FileCitation`s (file id and character `index`) of the same `ResponsesResult`, because the
  draft keeps citations deduplicated without positions. A2.5 gets both from A2.2's public
  `AnswerStep.request` / `responder` / `draft` without any change to A2.2.
- **L1 (GRD-01), deterministic, no call:** fails when the draft is a refusal or has no text; has
  no citation at all; has any unresolved citation (`unresolved_citations > 0`); has a citation
  or mark whose file is not a unit of this corpus (same file id, unit index, label, session and
  live corpus version); has a citation or mark outside the picked session; has a mark whose
  index lies outside the text; or when the top search score (max over the draft's hits; hits
  are not deduplicated by file id in A2.2 and a max is unaffected) is below
  `config.grounding_bar`. A picked session counts only hits in that session. No hits means
  `top_score = 0.0`, which fails (never a pass by default, L-12). When L1 fails the support call
  is skipped: no cost, `sentence_support = ()`.
- **Sentences (GRD-02/03):** the text is split deterministically. Each line is a block (a list
  marker `-`, `*`, `•`, `1.` or `1)` at its start is dropped); inside a line a sentence ends at
  `.`, `!` or `?` (plus closing quotes or brackets) followed by whitespace and an upper-case
  letter, digit, quote or bracket, or by the end of the line. A period after a known
  abbreviation (`e.g.`, `i.e.`, `etc.`, `vs.`, `p.`, `pp.`, `no.`, `fig.`, `dr.`, `mr.`, `mrs.`,
  `ms.`) or between digits does not end a sentence. A piece with no letter once text citation
  markers (`[1]`, `[1, 2]`, `【…】`) are removed is joined to the sentence before it.
- **Marks to sentences:** a mark belongs to the sentence whose span holds its index; an index in
  the gap after a sentence (the space, a newline, or the end of the text) belongs to the
  sentence before it; an index before the first sentence belongs to the first.
- **L2 (GRD-02):** one structured-output `respond` call, purpose `support`, the check model and its
  configured effort, no file search. The data block holds only the cited units (`u<index>` and
  their text) and the sentences that cite them (number, text, unit ids); never the question,
  the window or anything else, so nothing but cited text can support a claim. `SUPPORT_RULES`
  says to use no outside knowledge, that a sentence is supported only if every claim in it is
  stated in or follows directly from its listed units, and that data is never instruction.
  The reply is one `supported` boolean per sentence number sent, each exactly once.
- **Uncited sentences (GRD-03):** unsupported, never sent to the model, except the first uncited
  sentence that matches the connective allow-list exactly (case, spacing and a final `.`, `:`
  or `!` ignored): "Here is what the materials say", "Here is what the course materials say",
  "Here's what the materials say", "Here's what the course materials say", "According to the
  course materials", "The course materials cover this", "The materials cover this". A second
  connective is unsupported. Prefix matches never count, so "According to the course
  materials, X" is a factual sentence. If any uncited non-connective sentence exists the answer
  already fails, so the support call is skipped and cited sentences are reported `False`
  (in `sentence_support`, `True` means shown supported, never "not checked").
- **Failure rules:** L1 failing skips L2. A reply that will not parse (wrong shape, missing or
  repeated sentence numbers, refusal, or `LLMError` kind `parse`) is retried once, then fails
  closed: every checked sentence `False`. Any other `LLMError` propagates (Tech Spec §2.5).
- **Version:** `SUPPORT_RULES`, the JSON schema, the allow-list and a splitter version string are
  class constants hashed into `ground_version` (`ground-<12 hex>`), Tech Spec §4 and L-11.
- **GRD-04:** the public planted common-knowledge (B7) cases, answered from general knowledge in
  each of the ways a draft can be (no citation; citing a weakly matched unit below the bar;
  citing a unit above the bar that does not state the claim), never pass. The live run of all
  10 planted cases (5 are held out) needs the API and the A2.5 pipeline; it is recorded in
  RUN-STATE when run.

## Rules
- The step never edits or shows the draft; Finalize (A2.5) turns a failed result into
  `not_covered`.
- No unit or answer text reaches traces (N1.2 strips; this step adds no trace fields).
- `grounding_bar` and `SUPPORT_RULES` are tuned on the tuning split only (D-5); each change is a
  logged decision.
