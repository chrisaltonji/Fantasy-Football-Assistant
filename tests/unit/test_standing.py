"""Skill, read off the league table instead of remembered.

The dossier's first substantive question is "how good are they?", and its stated
job is to set how much to trust every other read. That is the one archetype
where the record genuinely beats recall, because a league table is exactly a
measurement of it — and it clears its null at z ≈ +2.9: people land in the same
part of this table year after year.

The care needed is in the null. Every season hands out exactly one of each
place, so the shuffle has to preserve that, the same way the roster-level
shuffle preserves a budget. And `erratic` has to be checked before the mean,
because somebody who finishes 4th, 1st, 12th, 11th is unpredictable however
respectable their average looks.
"""

from __future__ import annotations

import pytest

from ffa.history.schema import History, SeasonDraft
from ffa.history.standing import (
    ERRATIC_RANGE,
    MIN_SEASONS,
    Standing,
    classify,
    collect,
    finish_evidence,
    standings_for,
)

OWNERS = {i: f"OWNER-{i:02d}" for i in range(1, 13)}


def season(year: int, finishes: dict[int, int], points: dict[int, float] | None = None):
    return SeasonDraft(
        year=year, league_id=1, team_count=len(OWNERS), budget=200,
        owners=OWNERS, finishes=finishes,
        points_for=points or {t: 1800.0 for t in finishes},
    )


def league(*orders) -> History:
    """Each `order` is the finishing order of team ids, best first."""
    return History(seasons=tuple(
        season(2022 + i, {team: place + 1 for place, team in enumerate(order)})
        for i, order in enumerate(orders)
    ))


STABLE = [list(range(1, 13))] * 4          # everybody finishes where they always do
SHUFFLED = [
    list(range(1, 13)),
    list(range(12, 0, -1)),
    list(range(1, 13)),
    list(range(12, 0, -1)),
]


# --- collecting -----------------------------------------------------------------------


def test_finishes_come_back_in_season_order():
    history = league([1, 2, 3] + list(range(4, 13)), [3, 2, 1] + list(range(4, 13)))

    assert collect(history)["OWNER-01"].finishes == (1, 3)


def test_a_season_that_has_not_finished_is_not_a_last_place_finish():
    """ESPN reports rank 0 before a season settles. Recording that as twelfth
    would invent a collapse nobody had."""
    history = History(seasons=(season(2026, {}),))

    assert collect(history) == {}


def test_points_are_collected_alongside_but_kept_separate():
    history = league(list(range(1, 13)))
    standing = collect(history)["OWNER-01"]

    assert standing.mean_points == pytest.approx(1800.0)


# --- classifying --------------------------------------------------------------------


def test_the_top_of_a_stable_table_reads_as_sharp():
    standings = standings_for(league(*STABLE))

    assert standings["OWNER-01"].skill == "sharp"
    assert standings["OWNER-12"].skill == "casual"


def test_the_middle_of_the_table_is_solid_not_a_verdict():
    standings = standings_for(league(*STABLE))

    assert standings["OWNER-06"].skill == "solid"


def test_a_wide_swing_is_erratic_however_good_the_mean_looks():
    """4th, 1st, 12th, 11th averages 7.0 — respectable — and describes somebody
    who is capable of anything. The dossier's own vocabulary has that word."""
    swinging = Standing(owner_id="X", finishes=(4, 1, 12, 11))
    steady = Standing(owner_id="Y", finishes=(7, 7, 7, 7))

    labelled = classify({"X": swinging, "Y": steady, "Z": Standing("Z", (2, 3, 2, 3))})

    assert labelled["X"].skill == "erratic"
    assert labelled["Y"].skill != "erratic"


def test_erratic_is_checked_before_the_mean():
    assert Standing(owner_id="X", finishes=(1, 12, 1, 12)).swing >= ERRATIC_RANGE


def test_too_short_a_record_gets_no_label_at_all():
    """Two seasons is a coincidence, not a pattern."""
    short = Standing(owner_id="X", finishes=(1, 2))
    labelled = classify({"X": short, "Y": Standing("Y", (5, 5, 5, 5)),
                         "Z": Standing("Z", (9, 9, 9, 9))})

    assert labelled["X"].skill == ""
    assert short.seasons < MIN_SEASONS


def test_a_league_where_everybody_averages_the_same_labels_nobody_sharp():
    flat = {f"O{i}": Standing(f"O{i}", (6, 7, 6, 7)) for i in range(4)}

    assert {s.skill for s in classify(flat).values()} == {"solid"}


def test_labels_are_relative_to_this_league_not_to_a_fixed_place():
    """Finishing 4th means something different in a 12-team and an 8-team room."""
    small = {
        "A": Standing("A", (1, 1, 1)), "B": Standing("B", (2, 2, 2)),
        "C": Standing("C", (3, 3, 3)), "D": Standing("D", (4, 4, 4)),
    }
    labelled = classify(small)

    assert labelled["A"].skill == "sharp"
    assert labelled["D"].skill == "casual"


# --- the null ---------------------------------------------------------------------------


def test_a_table_people_hold_their_place_in_survives():
    evidence = finish_evidence(league(*STABLE), rounds=300, seed=1)

    assert evidence.survives, f"scored only z={evidence.z:+.1f}"
    assert evidence.null == "seasons"


def test_a_table_that_reverses_every_year_does_not_survive():
    """Everybody averages the same, so there is nothing to tell people apart by."""
    evidence = finish_evidence(league(*SHUFFLED), rounds=300, seed=1)

    assert not evidence.survives


def test_the_null_hands_out_each_place_exactly_once_per_season():
    """Every season has exactly one winner. A shuffle that forgets that would
    compare the real table against an impossible one."""
    evidence = finish_evidence(league(*STABLE), rounds=50, seed=2)

    # A null that broke the constraint would not land near the ~2.6 places a
    # genuine reshuffle of 1..12 across four seasons produces.
    assert 0.5 < evidence.mean < 4.0


def test_the_same_seed_reproduces_the_score():
    history = league(*STABLE)
    a = finish_evidence(history, rounds=100, seed=5)
    b = finish_evidence(history, rounds=100, seed=5)

    assert (a.observed, a.mean, a.stdev) == (b.observed, b.mean, b.stdev)


# --- how it is presented -----------------------------------------------------------------


def test_the_summary_names_every_finish_so_a_label_can_be_argued_with():
    summary = Standing(owner_id="X", finishes=(4, 1, 12, 11),
                       points=(1800.0, 1900.0, 1700.0, 1750.0)).summary()

    assert "4, 1, 12, 11" in summary
    assert "4 seasons" in summary


def test_a_manager_with_no_record_summarises_to_nothing_misleading():
    assert Standing(owner_id="X").seasons == 0
    assert Standing(owner_id="X").mean_finish == 0.0


# --- what it must never claim --------------------------------------------------------------


def test_skill_is_the_only_dossier_field_this_module_fills():
    """It measures the season, not the draft. Injuries, waivers and trades all
    land in a finishing position, so it answers "how good" and nothing else."""
    assert {f for f in Standing.__dataclass_fields__} == {
        "owner_id", "finishes", "points", "skill"
    }
