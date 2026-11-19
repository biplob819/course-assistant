"""D-86: the identity judge's public agreement monitor (no network)."""

from __future__ import annotations

import pytest

import identity_agreement as ia


def test_kappa_is_one_on_full_agreement_and_none_with_one_class():
    assert ia.kappa([("V", "V"), ("C", "C")]) == pytest.approx(1.0)
    assert ia.kappa([("C", "C")] * 5) is None  # chance agreement is total: no kappa
    assert ia.kappa([]) is None
    assert ia.kappa([("V", "C"), ("C", "V")]) == pytest.approx(-1.0)


def test_the_planted_replies_are_written_to_violate_and_the_rules_spare_handovers():
    assert len(ia.PLANTED) >= 5 and len(set(ia.PLANTED)) == len(ia.PLANTED)
    rules = " ".join(ia.IDENTITY_RULES.split())
    assert "passed to the teaching team" in rules and '"V" (violation)' in rules
