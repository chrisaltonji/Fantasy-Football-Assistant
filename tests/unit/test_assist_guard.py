"""The guard, which is where "a read, not a verdict" stops being a hope.

Everything else in this codebase guarantees itself structurally. Generated text
cannot, so the property is checked after parsing — and the tests below come in
matched pairs on purpose. Catching "Bid up to $40" is easy; the hard half is not
eating "a pass-catching back", and a guard that fires on ordinary football prose
would be turned off within one draft.
"""

from __future__ import annotations

import pytest

from ffa.assist.guard import (
    check_room,
    clamp_rivals,
    reject_verdict,
    verdict_matches,
)

THREATS = [
    {"team_id": 9, "max_legal_bid": 58, "is_live": True},
    {"team_id": 12, "max_legal_bid": 31, "is_live": True},
]


# --- what must be rejected --------------------------------------------------


@pytest.mark.parametrize("text", [
    "Bid up to $40 here.",
    "Pass. The room is thin at receiver.",
    "Don't chase him past $30.",
    "Do not let this one go.",
    "Take him at $22 — nobody else needs a tight end.",
    "Walk away above $45.",
    "The room is hot, so I'd bid $40.",
    "Given his gaps, I recommend bidding to $38.",
    "He fills your flex. You should bid.",
    "Let him go — you have two better options left.",
    "Nobody else is live. Go up to $12.",
])
def test_a_verdict_is_refused(text):
    assert reject_verdict(text) is not None, f"missed a verdict in {text!r}"


# --- what must survive, which is the half that matters ----------------------


@pytest.mark.parametrize("text", [
    "He is a pass-catching back, which is why indigo wants him.",
    "The room passed on three tight ends already tonight.",
    "Kyle passed at $30 last season and took the cheaper option.",
    "Two rivals have taken a receiver in the last four nominations.",
    "His sheet value takes the flex into account.",
    "Jordan bid $47 on the last receiver, eight over the sheet.",
    "That would be his third running back, and he has no flex left.",
    "Expect him above your advisable bid — this room pays up for receivers.",
    "The last elite back is gone, so the next tier takes the overflow.",
    "Avoidance of tight ends is the one thing his four seasons agree on.",
    "A bidding war here would be out of character for him.",
])
def test_ordinary_football_prose_survives(text):
    found = reject_verdict(text)
    assert found is None, f"false positive on {text!r} — matched {found!r}"


def test_empty_text_is_not_a_verdict():
    assert reject_verdict("") is None
    assert reject_verdict("   ") is None


# --- inference may never contradict arithmetic ------------------------------


def test_an_estimate_above_a_rivals_ceiling_is_dropped():
    """`max_legal_bid` is what is left after every other slot is charged $1. An
    estimate above it describes money that cannot be bid."""
    kept, dropped = clamp_rivals(
        [{"team_id": 9, "lo": 40, "hi": 70, "rationale": "front-loads"}], THREATS
    )

    assert kept == []
    assert dropped and "$58 ceiling" in dropped[0]


def test_an_estimate_at_the_ceiling_is_allowed():
    kept, _ = clamp_rivals(
        [{"team_id": 9, "lo": 50, "hi": 58, "rationale": "…"}], THREATS
    )
    assert len(kept) == 1


def test_a_team_that_is_not_a_threat_is_dropped():
    kept, dropped = clamp_rivals(
        [{"team_id": 4, "lo": 5, "hi": 9, "rationale": "…"}], THREATS
    )
    assert kept == []
    assert "not a threat" in dropped[0]


def test_an_inverted_band_is_dropped():
    kept, dropped = clamp_rivals(
        [{"team_id": 9, "lo": 40, "hi": 20, "rationale": "…"}], THREATS
    )
    assert kept == [] and "above hi" in dropped[0]


def test_an_unreadable_estimate_is_dropped_rather_than_raising():
    kept, dropped = clamp_rivals([{"team_id": "nine"}, {}], THREATS)
    assert kept == [] and len(dropped) == 2


def test_a_dropped_estimate_is_never_rewritten():
    """A corrected number would be a fabrication carrying the model's byline."""
    kept, _ = clamp_rivals(
        [{"team_id": 9, "lo": 40, "hi": 70}, {"team_id": 12, "lo": 8, "hi": 15}],
        THREATS,
    )
    assert [r["team_id"] for r in kept] == [12]
    assert kept[0]["hi"] == 15


# --- the Strategist has to agree with the arithmetic ------------------------


def test_a_strategist_that_disagrees_with_the_arithmetic_is_refused():
    """The three-state verdict is computed, not judged. Echoing it back exists
    solely so this check can exist."""
    assert verdict_matches("at risk", "at risk") is True
    assert verdict_matches("on track", "time to move") is False


def test_nothing_to_disagree_with_passes():
    assert verdict_matches("on track", None) is True


# --- the whole Room check ---------------------------------------------------


def test_check_room_strips_a_verdict_but_keeps_the_rest():
    parsed = {
        "read": "Bid up to $40 — he fills your flex.",
        "watch_for": ["indigo has no RB2", "Pass if it goes past $50"],
        "rivals": [{"team_id": 9, "lo": 30, "hi": 40, "rationale": "…"}],
    }

    cleaned, violations = check_room(parsed, THREATS)

    assert cleaned["read"] == ""                       # refused, not repaired
    assert cleaned["watch_for"] == ["indigo has no RB2"]
    assert len(cleaned["rivals"]) == 1                 # the good half survives
    assert len(violations) == 2


def test_a_clean_reply_passes_through_untouched():
    parsed = {
        "read": "Third receiver over sheet tonight; indigo front-loads and has "
                "taken every receiver to auction so far.",
        "watch_for": ["indigo has no WR2", "26 startable receivers left"],
        "rivals": [{"team_id": 12, "lo": 20, "hi": 28, "rationale": "…"}],
    }

    cleaned, violations = check_room(parsed, THREATS)

    assert violations == []
    assert cleaned["read"] == parsed["read"]
    assert cleaned["watch_for"] == parsed["watch_for"]
    assert cleaned["rivals"] == parsed["rivals"]
