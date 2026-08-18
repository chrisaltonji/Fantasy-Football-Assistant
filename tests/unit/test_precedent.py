"""A manager's own spending script, and the rules that keep it honest.

This is the one thing four prior auctions tell you that tonight's board cannot:
not what a rival *can* spend — `max_legal_bid` already knows that to the dollar —
but whether the number in front of you is normal for that person at this point.

Three properties are load-bearing and all three are pinned here:

- it goes quiet once the record stops discriminating, which it measures rather
  than assumes;
- it stays quiet when a manager's own seasons disagree by more than tonight
  departs from them;
- it never touches the bid arithmetic.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from ffa.advice.types import PaceRead, Threat
from ffa.history.precedent import (
    DECILES,
    ManagerPrecedent,
    PrecedentBook,
    discriminating_window,
    load_precedent,
    save_precedent,
)


def script(curve, spread=None, seasons=(2022, 2023, 2024, 2025)) -> ManagerPrecedent:
    return ManagerPrecedent(
        owner_id="ANN", manager="Ann", seasons=tuple(seasons), curve=tuple(curve),
        spread=tuple(spread if spread is not None else [0.0] * DECILES),
    )


LINEAR = [(i + 1) / 10 for i in range(DECILES)]


# --- the curve ----------------------------------------------------------------------


@pytest.mark.parametrize("progress", [0.0, 0.05, 0.1, 0.15, 0.3, 0.5, 0.95, 1.0])
def test_the_curve_is_anchored_at_the_origin(progress):
    """`curve[i]` is the value at the *end* of decile i.

    Treating it as the value at the start shifts everything a whole decile
    early — which reported a manager $148 behind a script they were exactly on.
    """
    assert script(LINEAR).expected_share(progress) == pytest.approx(progress)


def test_progress_outside_the_board_is_clamped():
    curve = script(LINEAR)
    assert curve.expected_share(-1.0) == pytest.approx(0.0)
    assert curve.expected_share(5.0) == pytest.approx(1.0)


def test_a_manager_with_no_curve_has_no_expectation():
    assert ManagerPrecedent(owner_id="X").expected_share(0.2) is None


def test_two_seasons_is_flagged_as_thin():
    assert script(LINEAR, seasons=(2024, 2025)).is_thin is True
    assert script(LINEAR).is_thin is False


# --- the window is measured, not assumed --------------------------------------------


def test_the_window_closes_where_managers_stop_differing():
    """An auction front-loads by construction. Past the point where everybody
    has spent nearly everything there is nothing left to distinguish, and a
    confident readout there is noise delivered under time pressure."""
    early_spread = {
        "a": [[0.2, 0.5, 0.9, 1.0], [0.2, 0.5, 0.9, 1.0]],
        "b": [[0.6, 0.8, 0.95, 1.0], [0.6, 0.8, 0.95, 1.0]],
    }
    window = discriminating_window(early_spread)

    assert 0 < window <= 1.0


def test_a_league_where_everybody_drafts_alike_has_no_window():
    same = {name: [[0.5, 0.9, 1.0], [0.5, 0.9, 1.0]] for name in ("a", "b", "c")}
    assert discriminating_window(same) == 0.0


def test_one_season_is_not_enough_to_measure_a_window():
    assert discriminating_window({"a": [[0.5]], "b": [[0.9]]}) == 0.0


def test_the_book_reports_nothing_past_its_own_window():
    book = PrecedentBook(window=0.3, managers={"ANN": script(LINEAR)})

    assert book.discriminates_at(0.2) is True
    assert book.discriminates_at(0.3) is True
    assert book.discriminates_at(0.31) is False


def test_nothing_is_claimed_about_the_opening_picks():
    """Before the curves diverge, expected share and its uncertainty are both
    near zero, so buying one player reads as being far "ahead of script". A dry
    run printed exactly that: `6% of budget out, usually 0% by now`. Nobody is
    ahead of anything at pick four — there is no script yet to be ahead of.
    """
    book = PrecedentBook(window=0.3, managers={"ANN": script(LINEAR)})

    assert book.discriminates_at(0.01) is False
    assert book.discriminates_at(0.04) is False
    assert book.discriminates_at(0.05) is True


def test_a_book_with_no_window_never_claims_to_discriminate():
    assert PrecedentBook(managers={"ANN": script(LINEAR)}).discriminates_at(0.1) is False


# --- what counts as worth saying ------------------------------------------------------


def read(actual, expected, wobble=0.0, budget=200) -> PaceRead:
    return PaceRead(
        team_id=1, label="ann", spent=int(actual * budget), budget=budget,
        actual_share=actual, expected_share=expected, wobble=wobble, seasons=4,
    )


def test_dollars_are_signed_so_behind_and_ahead_are_distinguishable():
    assert read(0.30, 0.60).dollars_vs_script == -60   # money still in hand
    assert read(0.90, 0.60).dollars_vs_script == +60   # spent early


def test_a_small_departure_is_not_worth_interrupting_a_draft():
    assert read(0.52, 0.50).is_notable is False


def test_a_departure_inside_the_managers_own_wobble_is_not_a_finding():
    """Rudner's four seasons span 40% of budget at the second decile. A 19%
    departure from his mean is not him behaving unusually — it is the range he
    always had, and reporting it would invent precision the record lacks."""
    assert read(0.30, 0.50, wobble=0.35).is_notable is False


def test_a_departure_that_clears_the_wobble_is_reported():
    assert read(0.20, 0.65, wobble=0.10).is_notable is True


def test_a_perfectly_consistent_manager_still_needs_a_real_gap():
    """Zero wobble must not make every $1 difference notable."""
    assert read(0.51, 0.50, wobble=0.0).is_notable is False
    assert read(0.70, 0.50, wobble=0.0).is_notable is True


# --- the boundary with tonight's arithmetic --------------------------------------------


def test_threat_carries_no_history_and_says_so():
    """`Threat` is tonight's capacity and nothing else. A measurement from 2023
    inside it would blur the line the whole advice module rests on."""
    names = {f.name for f in fields(Threat)}

    assert names == {
        "team_id", "label", "remaining", "max_legal_bid",
        "remaining_is_floor", "has_starter_gap", "open_at_position",
    }
    assert "Capacity, not intent" in Threat.__doc__


def test_precedent_never_moves_the_bid_arithmetic(init_event, tmp_path):
    """Delete every PaceRead and the numbers you act on are unchanged.

    This is the guarantee that lets history sit beside the advisory layer
    instead of inside it.
    """
    from pathlib import Path as _P

    from ffa.advice.engine import advise
    from ffa.domain import reducers
    from ffa.reference.loader import LoadReport, ReferenceRow
    from ffa.reference.playerbook import PlayerBook
    from ffa.domain.enums import Position
    from ffa.domain.ids import normalize_player_key
    from tests.conftest import sold

    rows = [
        ReferenceRow(key=normalize_player_key(f"P{i}"), name=f"P{i}",
                     position=Position.RB, auction_value=float(40 - i % 30), overall_rank=i)
        for i in range(1, 40)
    ]
    book = PlayerBook.from_report(LoadReport(source=_P("t"), rows=tuple(rows)))
    # Past MIN_PROGRESS: 12 teams x 15 slots is a 180-pick board, so the first
    # nine sales are the opening 5% where nothing is claimed.
    state = reducers.replay(
        [init_event] + [sold(i + 2, f"P{i}", (i % 11) + 1, 12) for i in range(1, 19)]
    )

    seats = {t: f"OWNER-{t:02d}" for t in state.teams}
    curve = script(LINEAR)
    book_with = PrecedentBook(window=1.0, managers={s: curve for s in seats.values()})

    without = advise(state, book, key="p1")
    with_history = advise(state, book, key="p1", precedent=book_with, seats=seats)

    assert with_history.guidance.max_advisable_bid == without.guidance.max_advisable_bid
    assert with_history.guidance.max_legal_bid == without.guidance.max_legal_bid
    assert with_history.guidance.safe_legal_bid == without.guidance.safe_legal_bid
    assert with_history.guidance.suggested_high == without.guidance.suggested_high
    assert with_history.guidance.threats == without.guidance.threats
    # …and the only difference is the new, parallel field.
    assert with_history.guidance.pace_reads and not without.guidance.pace_reads


# --- the artifact -----------------------------------------------------------------------


def test_a_book_round_trips_through_disk(tmp_path: Path):
    path = tmp_path / "precedent.json"
    book = PrecedentBook(
        league_id=7, seasons=(2024, 2025), window=0.3,
        managers={"ANN": script(LINEAR, seasons=(2024, 2025))},
    )
    save_precedent(book, path)

    loaded = load_precedent(path, league_id=7)

    assert loaded.window == pytest.approx(0.3)
    assert loaded.for_owner("ann").curve == tuple(LINEAR)
    assert loaded.for_owner("{ANN}") is not None  # folded like every other SWID


def test_a_missing_artifact_costs_the_readout_and_not_the_draft(tmp_path: Path):
    book = load_precedent(tmp_path / "nope.json")

    assert not book
    assert book.warnings == ()


def test_an_unreadable_artifact_degrades_with_a_warning(tmp_path: Path):
    path = tmp_path / "precedent.json"
    path.write_text("{not json", encoding="utf-8")

    book = load_precedent(path)

    assert not book
    assert any("could not read" in w for w in book.warnings)


def test_an_artifact_from_another_league_is_refused():
    """It would describe strangers with total confidence."""
    import json

    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.json"
        path.write_text(json.dumps({"league_id": 111, "managers": {}}), encoding="utf-8")

        book = load_precedent(path, league_id=999)

        assert not book
        assert any("was built for league 111" in w for w in book.warnings)


def test_a_manager_with_a_malformed_curve_is_skipped_not_guessed_at(tmp_path: Path):
    import json

    path = tmp_path / "p.json"
    path.write_text(
        json.dumps({"league_id": 1, "managers": {"ANN": {"curve": [0.1, 0.2]}}}),
        encoding="utf-8",
    )

    book = load_precedent(path, league_id=1)

    assert book.for_owner("ANN") is None
    assert any("no usable curve" in w for w in book.warnings)


# --- degradation -------------------------------------------------------------------------


def test_a_manager_with_no_record_produces_no_read(init_event):
    from ffa.advice.bidding import pace_reads_for
    from ffa.domain import reducers

    state = reducers.replay([init_event])
    book = PrecedentBook(window=1.0, managers={})

    assert pace_reads_for(state, book, {}, exclude_team=None) == ()


def test_no_precedent_at_all_is_simply_nothing(init_event):
    from ffa.advice.bidding import pace_reads_for
    from ffa.domain import reducers

    assert pace_reads_for(reducers.replay([init_event]), None, {}) == ()
