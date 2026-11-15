# A2.2 acceptance criteria

**ANS-01** `AnswerStep` makes one `respond` call with purpose `answer`, the answer model and its configured effort, file search on the cohort store with `max_num_results` from config and results included, and returns a `Draft`.
**ANS-02** The request always filters on the live corpus version; with a picked session it is one compound `and` of corpus version and session; an unknown session is refused before any call.
**ANS-03** Every `file_citation` maps to a `Citation` with the unit's index, label and session, deduplicated in order; citations and hits outside the live corpus are excluded and counted.
**ANS-04** `ANSWER_RULES` (with its canary) is a class constant hashed into `rules_version`; question and window travel as one escaped JSON document in data delimiters; earlier assistant replies carry no citations.
