"""E2.5 quality round (OWNER D-71): citation handling found by diagnosing the public misses.

1. In-text provider citation tokens. The answer model sometimes writes a citation into the text
   as private-use characters (U+E200 "filecite" U+E202 "turn0fileK" ... U+E201) instead of an
   annotation. They reached the learner as unreadable characters, merged two sentences for the
   splitter, and left the sentence before them uncited. They are now removed from the text and
   each part becomes a citation of the K-th file search result; anything that cannot be mapped
   exactly becomes an unresolved citation, so Ground L1 fails (fail closed).
2. Paragraph-end citations. The provider often puts one citation after the last sentence of a
   paragraph. An uncited sentence now takes the citations of the next cited sentence on the same
   line (a lead-in ending with ":" also takes those of the next line), and Ground L2 judges it
   against those units like any cited sentence. A trailing uncited sentence still fails with no
   support call, and the one allowed connective is still never sent.
3. The answer and support rules state the citation and paraphrase rules plainly (versioned).
"""

from __future__ import annotations

import pytest

from course_assistant.answer import responder
from course_assistant.answer.answer import (
    INLINE_CITATIONS,
    UNRESOLVED_INLINE,
    AnswerStep,
    resolve_inline,
)
from course_assistant.answer.ground import GroundStep, inherit_citations, split_sentences
from course_assistant.answer.hint import HintStep
from course_assistant.llm import FileCitation, Hit, ResponsesResult
from tests.answer import scripted as s
from tests.answer.test_ask import CONFIG, CORPUS, PID, PRICES, QUESTION, S01, make
from tests.answer.test_ground import (
    BAR,
    ONE,
    S02,
    S02P,
    TWO,
    drafted,
    end,
    ground_with,
    verdicts,
)
from tests.answer.test_ground import body as support_body

OPEN, SEP, CLOSE = "", "", ""


def token(*parts: str, kind: str = "filecite") -> str:
    return OPEN + SEP.join((kind, *parts)) + CLOSE


def result(text: str, cites: tuple[tuple[str, int], ...] = (),
           hits: tuple[str, ...] = (S02, S02P), searches: int = 1) -> ResponsesResult:  # fmt: skip
    return ResponsesResult(text=text, citations=tuple(FileCitation(file_id=f, index=i)
                                                      for f, i in cites),
                           hits=tuple(Hit(file_id=f, score=0.8) for f in hits),
                           refusal=False, model_snapshot=s.SOL, tokens_in=1, tokens_cached=0,
                           tokens_out=1, tokens_reasoning=0, file_search_calls=searches,
                           cost_usd=0.001, latency_ms=10, request_id="r-1",
                           attempts=1)  # fmt: skip


# ---------- 1. in-text provider citation tokens ----------


def test_a_result_without_tokens_is_returned_unchanged():
    r = result(f"{ONE} {TWO}", ((S02, len(ONE)),))
    assert resolve_inline(r) is r


def test_a_token_is_removed_and_each_part_cites_that_search_result_at_its_place():
    text = f"{ONE} {token('turn0file1', 'turn0file0')} {TWO}"
    out = resolve_inline(result(text))
    assert out.text == f"{ONE} {TWO}"
    assert [(c.file_id, c.index) for c in out.citations] == [(S02P, len(ONE)), (S02, len(ONE))]
    assert not any(ch in out.text for ch in (OPEN, SEP, CLOSE))


def test_annotation_indexes_after_a_token_move_with_the_text():
    text = f"{ONE} {token('turn0file0')} {TWO}"
    after = len(text)  # an annotation at the end of the second sentence
    out = resolve_inline(result(text, ((S02P, after),)))
    assert out.text == f"{ONE} {TWO}"
    by_file = {c.file_id: c.index for c in out.citations}
    assert by_file == {S02: len(ONE), S02P: len(out.text)}


def test_a_trailing_token_cites_the_last_sentence():
    text = f"{ONE} {token('turn0file0')}"
    out = resolve_inline(result(text))
    assert out.text == ONE and [(c.file_id, c.index) for c in out.citations] == [(S02, len(ONE))]


@pytest.mark.parametrize("make_text,searches", [
    (lambda: f"{ONE} {token('turn0file7')}", 1),  # no such result
    (lambda: f"{ONE} {token('turn1file0')}", 1),  # another turn
    (lambda: f"{ONE} {token('turn0file0')}", 2),  # two searches: which list is meant is unknown
    (lambda: f"{ONE} {token('turn0file0', kind='navlist')}", 1),  # not a file citation
    (lambda: f"{ONE} {token('turn0fileX')}", 1),  # malformed part
    (lambda: f"{ONE} {OPEN}filecite{SEP}turn0fi", 1),  # cut off before its end
])  # fmt: skip
def test_a_token_that_cannot_be_mapped_exactly_is_unresolved_and_fails_l1(make_text, searches):
    out = resolve_inline(result(make_text(), searches=searches))
    assert out.text == ONE
    assert any(c.file_id == UNRESOLVED_INLINE for c in out.citations)
    step = AnswerStep(CONFIG, CORPUS, lambda r: out)
    draft = step.draft(out)
    assert draft.unresolved_citations >= 1
    ground = GroundStep(CONFIG, CORPUS, lambda r: out)
    assert not ground.citations_resolve(draft, out.citations, None)


def test_stray_private_use_separators_never_reach_the_text():
    out = resolve_inline(result(f"{ONE}{CLOSE} {SEP}{TWO}", ((S02, len(ONE)),)))
    assert out.text == f"{ONE} {TWO}"


def test_the_inline_rule_is_hashed_into_the_answer_version(monkeypatch):
    before = AnswerStep.rules_version()
    monkeypatch.setattr("course_assistant.answer.answer.INLINE_CITATIONS", INLINE_CITATIONS + "x")
    assert AnswerStep.rules_version() != before


def test_an_answer_with_only_a_token_is_answered_with_that_citation_and_clean_text():
    text = f"Name the person who has the problem first. {token('turn0file0')}"
    a, client, _, _ = make(route=[s.route("course")],
                           answer=[s.sol(text, (), ((S01, 0.9),))],
                           support=[s.support(True)], integrity=[s.integrity("clear")])  # fmt: skip
    ans = a.turn(QUESTION, PID).answer
    assert ans.status == "answered" and ans.text == "Name the person who has the problem first."
    assert [c.file_id for c in ans.citations] == [S01]
    integrity_call = next(kw for st, kw in client.calls if st == "integrity")
    assert OPEN not in str(integrity_call["input"])


def test_a_hint_never_shows_a_token():
    text = f"Revisit session one on naming the problem. {token('turn0file0')}"
    hint = s.sol(text, (), ((S01, 0.9),))
    a, _, _, _ = make(route=[s.route("graded_work", "x", "s01-a1")],
                      answer=[s.cited("d", S01)], hint=[hint],
                      integrity=[s.integrity("clear")])  # fmt: skip
    ans = a.turn("Write my brief for Assignment 1", PID).answer
    assert ans.status == "hint_only" and OPEN not in ans.text and CLOSE not in ans.text
    assert ans.text == "Revisit session one on naming the problem."
    assert [c.file_id for c in ans.citations] == [S01]


# ---------- 2. paragraph-end citations ----------


def test_an_uncited_sentence_takes_the_citation_of_the_next_sentence_on_its_line():
    text = f"{ONE} {TWO}"
    draft, marks = drafted(text=text, cites=((S02P, len(text)),))
    step, client = ground_with([verdicts(True, True)])
    report = step.assess(draft, marks)
    assert report.result.passed and len(client.responses.calls) == 1
    sent = support_body(client.responses.calls[0])["sentences"]
    assert [x["units"] for x in sent] == [["u3"], ["u3"]]


def test_an_inherited_sentence_the_check_rejects_fails_the_answer():
    text = f"{ONE} {TWO}"
    draft, marks = drafted(text=text, cites=((S02P, len(text)),))
    step, _ = ground_with([verdicts(False, True)])
    assert not step.run(draft, marks).passed


def test_a_trailing_uncited_sentence_still_fails_without_a_support_call():
    text = f"{ONE} Most teams also use A/B tests."
    draft, marks = drafted(text=text, cites=((S02, end(text, ONE)),))
    step, client = ground_with([])
    report = step.assess(draft, marks)
    assert report.result.sentence_support == (False, False) and not report.result.passed
    assert client.responses.calls == []


def test_a_citation_on_the_next_line_does_not_cover_a_sentence_without_a_colon():
    text = f"{ONE}\n{TWO}"
    draft, marks = drafted(text=text, cites=((S02P, len(text)),))
    step, client = ground_with([])
    assert not step.run(draft, marks).passed and client.responses.calls == []


def test_a_lead_in_ending_with_a_colon_takes_the_citation_of_the_next_line():
    text = f"The materials list two points:\n- {ONE}\n- {TWO}"
    cites = ((S02, end(text, ONE)), (S02P, len(text)))
    draft, marks = drafted(text=text, cites=cites)
    step, client = ground_with([verdicts(True, True, True)])
    assert step.run(draft, marks).passed
    sent = support_body(client.responses.calls[0])["sentences"]
    assert [x["units"] for x in sent] == [["u2"], ["u2"], ["u3"]]


def test_the_allowed_connective_is_still_exempt_and_never_sent():
    text = f"Here is what the materials say. {ONE}"
    draft, marks = drafted(text=text, cites=((S02, len(text)),))
    step, client = ground_with([verdicts(True)])
    assert step.run(draft, marks).passed
    sent = support_body(client.responses.calls[0])["sentences"]
    assert [x["text"] for x in sent] == [ONE]


def test_inherit_citations_is_a_pure_function_of_text_spans_and_own_citations():
    text = "A one. B two. C three.\nD four: \n\nE five. F six."
    spans = split_sentences(text)
    own = [set(), set(), {1}, set(), {2}, set()]
    got = inherit_citations(text, spans, own, exempt=None)
    assert got == [{1}, {1}, {1}, {2}, {2}, set()]
    assert own == [set(), set(), {1}, set(), {2}, set()]  # the input is not changed
    assert inherit_citations(text, spans, own, exempt=0)[0] == set()


def test_the_attribution_rule_is_hashed_into_the_ground_version(monkeypatch):
    before = GroundStep.version()
    monkeypatch.setattr(GroundStep, "ATTRIBUTION", GroundStep.ATTRIBUTION + "x")
    assert GroundStep.version() != before


@pytest.mark.safety
def test_an_inherited_general_knowledge_sentence_is_still_judged_and_can_fail():
    """A planted answer cannot pass by riding on a later citation: L2 judges it (B7)."""
    text = f"Paris is the capital of France. {ONE}"
    draft, marks = drafted(text=text, cites=((S02, len(text)),))
    step, client = ground_with([verdicts(False, True)])
    report = step.assess(draft, marks)
    assert not report.result.passed and report.result.top_score >= BAR
    sent = support_body(client.responses.calls[0])["sentences"]
    assert sent[0]["text"] == "Paris is the capital of France." and sent[0]["units"] == ["u2"]


# ---------- 3. the rules ----------


def test_the_answer_rules_ask_for_a_citation_per_sentence_and_no_additions():
    rules = AnswerStep.ANSWER_RULES.lower()
    for phrase in ("every sentence", "its own citation", "do not add",
                   "reasons, consequences, contrasts, examples"):  # fmt: skip
        assert phrase in rules
    assert AnswerStep.CANARY in AnswerStep.ANSWER_RULES


def test_the_answer_rules_forbid_a_bare_yes_or_no_and_remarks_on_what_is_left_out():
    # E2.5 quality round on terra (public half): an uncited "Yes." or "The materials do not
    # explain why." failed sentence support and turned a covered answer into not_covered
    rules = " ".join(AnswerStep.ANSWER_RULES.lower().split())
    assert 'do not open with a bare "yes." or "no."' in rules
    assert "write no sentence about what the materials leave out" in rules
    # ...and the second public run answered two B2 questions from a related topic instead
    assert "material on a related topic is not an answer" in rules


def test_the_support_rules_accept_faithful_paraphrase_but_still_forbid_additions():
    rules = GroundStep.SUPPORT_RULES.lower()
    assert "judge the meaning, not the wording" in rules and "faithful paraphrase" in rules
    assert "outside knowledge" in rules and "is unsupported" in rules
    assert "adds a\nnumber" in rules or "adds a number" in rules.replace("\n", " ")


def test_the_hint_rules_change_only_by_a_recorded_decision():
    # D-71 kept the hint rules (hint-588d167c5876); D-87 adds "say where to look, not what it
    # says". Any other change to the hint rules must move this pin with its own decision row
    assert HintStep.hint_version() == "hint-dadb39174cbe"
    assert "Never write, draft, outline, fill in, complete" in HintStep.HINT_RULES


def test_a_responder_built_answer_step_resolves_tokens_in_run():
    text = f"{ONE} {token('turn0file0')}"
    client = s.StepClient(answer=[s.sol(text, (), ((S02, 0.8),))])
    rsp = responder(client=client, prices=PRICES, sleep=lambda _: None)
    from tests.answer.test_ground import CONFIG as GCONFIG
    from tests.answer.test_ground import CORPUS as GCORPUS

    draft = AnswerStep(GCONFIG, GCORPUS, rsp).run("What gates a release?")
    assert draft.text == ONE and [c.file_id for c in draft.citations] == [S02]
