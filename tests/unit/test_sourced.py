"""Tests for the merge rule.

If any single test file in this project is worth reading, it's this one. The
merge rule is what makes the ESPN path and the manual path interchangeable
per-field, and it has two failure modes that produce plausible-looking wrong
numbers rather than crashes.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from ffa.domain.enums import Provenance
from ffa.domain.sourced import Sourced, known, merge, unknown

T0 = datetime(2026, 8, 16, 19, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=30)

ALL = list(Provenance)
ORDERED_PAIRS = [(a, b) for a, b in itertools.permutations(ALL, 2)]


def s(value, provenance, at=T0, **kw) -> Sourced:
    return Sourced(value, provenance, at, **kw)


# --- rules 3-4: both known, rank decides -------------------------------------


@pytest.mark.parametrize("lower,higher", [(a, b) for a, b in ORDERED_PAIRS if a.rank < b.rank])
def test_higher_provenance_wins_from_either_direction(lower, higher):
    low, high = s(1, lower), s(2, higher)
    assert merge(low, high) is high
    assert merge(high, low) is high


def test_manual_outranks_espn_so_a_correction_is_never_stomped():
    corrected = s(45, Provenance.MANUAL, T0)
    later_poll = s(47, Provenance.ESPN_API, T1)
    assert merge(corrected, later_poll) is corrected


# --- rules 1-2: knowledge beats ignorance ------------------------------------


@pytest.mark.parametrize("known_prov,unknown_prov", ORDERED_PAIRS)
def test_known_always_beats_unknown_regardless_of_rank(known_prov, unknown_prov):
    """Rule 2. Omitting this is the bug that silently breaks partial fallback."""
    has_value = s(45, known_prov, T0)
    no_value = s(None, unknown_prov, T1)
    assert merge(no_value, has_value) is has_value
    assert merge(has_value, no_value) is has_value


def test_the_flagship_scenario():
    """`sold mahomes ? t3` then ESPN reports $45. The $45 must win.

    Under a naive rule (provenance first, MANUAL > ESPN_API) the manual
    unknown would outrank the real price forever.
    """
    typed_by_hand = unknown(T0)
    from_espn = known(45, Provenance.ESPN_API, T1)
    assert merge(typed_by_hand, from_espn).value == 45


def test_both_unknown_falls_through_to_rank():
    assert merge(s(None, Provenance.UNKNOWN), s(None, Provenance.MANUAL)).provenance is (
        Provenance.MANUAL
    )


# --- rule 5: equal rank -------------------------------------------------------


def test_same_provenance_newer_wins():
    old, new = s(10, Provenance.MANUAL, T0), s(20, Provenance.MANUAL, T1)
    assert merge(old, new) is new
    assert merge(new, old) is new


def test_equal_timestamps_resolve_to_incoming_ie_journal_order():
    first, second = s(10, Provenance.MANUAL, T0), s(20, Provenance.MANUAL, T0)
    assert merge(first, second) is second


# --- structural guarantees ----------------------------------------------------


def test_merge_against_none_returns_incoming():
    incoming = s(5, Provenance.MANUAL)
    assert merge(None, incoming) is incoming


def test_merge_is_idempotent_so_a_double_poll_is_harmless():
    a, b = s(10, Provenance.MANUAL, T0), s(20, Provenance.ESPN_API, T1)
    once = merge(a, b)
    assert merge(once, b) == once


def test_merge_never_mutates_its_inputs():
    a, b = s(10, Provenance.MANUAL, T0), s(20, Provenance.ESPN_API, T1)
    before = (a.value, a.provenance, b.value, b.provenance)
    merge(a, b)
    assert (a.value, a.provenance, b.value, b.provenance) == before


def test_confidence_does_not_affect_merge():
    """Pinned deliberately so nobody 'improves' merge by weighting confidence.

    Confidence exists to be shown to the user and to let CP3 flag a shaky
    fuzzy match — not to reorder precedence behind their back.
    """
    low = s(10, Provenance.MANUAL, T0, confidence=0.1)
    high = s(20, Provenance.ESPN_API, T1, confidence=1.0)
    assert merge(low, high) is low


# --- constructors and serialization ------------------------------------------


def test_unknown_is_tagged_unknown_not_the_reporters_provenance():
    u = unknown(T0)
    assert u.provenance is Provenance.UNKNOWN
    assert u.confidence == 0.0
    assert not u.is_known


def test_round_trip():
    original = known(45, Provenance.MANUAL, T0, confidence=0.9, note="typed")
    assert Sourced.from_obj(original.to_obj()) == original


def test_defaults_are_omitted_from_the_wire_format():
    obj = known(45, Provenance.MANUAL, T0).to_obj()
    assert obj == {"value": 45, "provenance": "MANUAL", "observed_at": "2026-08-16T19:00:00Z"}


def test_unknown_value_serializes_as_an_explicit_null():
    obj = unknown(T0).to_obj()
    assert "value" in obj and obj["value"] is None


def test_decoder_is_not_run_on_a_null_value():
    def explode(_):  # pragma: no cover - must never be called
        raise AssertionError("decoder ran on None")

    assert Sourced.from_obj(unknown(T0).to_obj(), decode=explode).value is None


def test_unrecognized_provenance_demotes_rather_than_raising():
    obj = known(45, Provenance.MANUAL, T0).to_obj() | {"provenance": "FROM_THE_FUTURE"}
    assert Sourced.from_obj(obj).provenance is Provenance.UNKNOWN
