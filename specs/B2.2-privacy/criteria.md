# B2.2 acceptance criteria

**PRV-01** A learner without consent for the current notice version needs the notice; changing the version asks again; the notice text states what is stored, why, for how long, where it goes (OpenAI, store off) and how to delete it.
**PRV-02** Consent is recorded per purpose; `allowed` is false after withdrawal and for a purpose never given; give and withdraw are audited.
**PRV-03** History returns only the learner's own conversations, questions, answers and replies, in order.
**PRV-04** Delete removes the learner's app rows and trace lines (whole history or one conversation), records trace completion, and leaves other learners untouched; a request with traces still present after 7 days is flagged by purge.
