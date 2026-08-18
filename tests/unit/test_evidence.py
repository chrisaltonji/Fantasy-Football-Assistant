"""The permutation harness — because the harness itself got this wrong once.

Four seasons of twelve managers is about fifteen picks each. That is enough to
rank people and nowhere near enough to stop a plausible pattern appearing on its
own, so every claim this codebase makes is scored against a null.

The failure worth pinning is not "a signal was missed". It is **the null being
more permissive than the process that produced the data**. Shuffling individual
picks between managers lets an imaginary manager hold three $70 players, which
is impossible under a $200 budget — and under that null a real pace effect
scored z = −1.7, a spread *narrower* than random. A real effect can be absent;
it cannot make a population more uniform than shuffling does. The load-bearing
test here plants an effect the budget forbids the wrong null from seeing, and
asserts the harness reports on itself rather than on the data.
"""

from __future__ import annotations

import pytest

from ffa.history.evidence import (
    Evidence,
    actual,
    between_manager_spread,
    permutation,
    shuffle_picks,
    shuffle_rosters,
)
from ffa.history.schema import DraftPick, History, SeasonDraft

OWNERS = {1: "ANN", 2: "BOB", 3: "CAT", 4: "DEE"}
BUDGET = 200


def season(year: int, spend_by_team, position="RB") -> SeasonDraft:
    """`spend_by_team: {team_id: [prices]}` laid out in board order."""
    picks, n = [], 1
    for team, prices in sorted(spend_by_team.items()):
        for price in prices:
            picks.append(
                DraftPick(
                    overall_pick=n, team_id=team, player_id=n, price=price,
                    position=position, pro_team="KC", nominating_team_id=team,
                    reference=float(price),
                )
            )
            n += 1
    return SeasonDraft(
        year=year, league_id=1, team_count=len(spend_by_team), budget=BUDGET,
        picks=tuple(picks), owners=OWNERS,
    )


def total_spend(picks, _year):
    return sum(p.price for p in picks) / BUDGET


def top_buy(picks, _year):
    return (max(p.price for p in picks) / BUDGET) if picks else None


# --- the record itself ----------------------------------------------------------------


def test_z_is_distance_from_the_shuffled_mean():
    ev = Evidence(name="x", null="rosters", observed=0.30, mean=0.10, stdev=0.05)
    assert ev.z == pytest.approx(4.0)
    assert ev.survives is True
    assert ev.verdict == "real"


def test_a_flat_null_does_not_divide_by_zero():
    ev = Evidence(name="x", null="rosters", observed=0.3, mean=0.3, stdev=0.0)
    assert ev.z == 0.0
    assert ev.survives is False


def test_an_ordinary_negative_z_is_just_absence_not_a_broken_test():
    """Under a correct null with no effect, z is roughly standard normal, so
    mildly negative values are entirely expected."""
    ev = Evidence(name="x", null="rosters", observed=0.09, mean=0.10, stdev=0.02)

    assert ev.suspect is False
    assert ev.verdict == "not distinguishable from chance"


def test_a_spread_far_narrower_than_chance_is_reported_as_a_broken_test():
    """This is how the pace mis-specification announced itself.

    A real effect can be absent; it cannot make a population markedly more
    uniform than shuffling it does.
    """
    ev = Evidence(name="x", null="picks", observed=0.02, mean=0.10, stdev=0.02)

    assert ev.suspect is True
    assert "check the null" in ev.verdict


# --- the two shuffles ------------------------------------------------------------------


def test_the_roster_shuffle_keeps_every_roster_intact():
    """Which is the whole point: a shuffled manager still holds a roster
    somebody actually built, under a real budget."""
    import random

    draft = season(2024, {1: [100, 50, 25], 2: [40] * 3, 3: [10] * 3, 4: [5] * 3})
    history = History(seasons=(draft,))

    shuffled = shuffle_rosters(history, random.Random(1))
    groups: dict[str, list[int]] = {}
    for (_year, i), owner in shuffled.items():
        groups.setdefault(owner, []).append(draft.picks[i].price)

    assert sorted(sorted(v) for v in groups.values()) == sorted(
        sorted(v) for v in ([100, 50, 25], [40] * 3, [10] * 3, [5] * 3)
    )


def test_the_pick_shuffle_deliberately_breaks_rosters_apart():
    """Correct for affinity questions, and exactly why it is wrong for budgets."""
    import random

    draft = season(2024, {1: [100, 50, 25], 2: [40] * 3, 3: [10] * 3, 4: [5] * 3})
    shuffled = shuffle_picks(History(seasons=(draft,)), random.Random(1))

    assert sorted(shuffled.values()) == sorted(actual(History(seasons=(draft,))).values())


def test_both_shuffles_preserve_how_many_picks_each_manager_holds():
    import random

    draft = season(2024, {1: [10] * 3, 2: [20] * 3, 3: [30] * 3, 4: [40] * 3})
    history = History(seasons=(draft,))

    for shuffler in (shuffle_rosters, shuffle_picks):
        counts: dict[str, int] = {}
        for owner in shuffler(history, random.Random(2)).values():
            counts[owner] = counts.get(owner, 0) + 1
        assert set(counts.values()) == {3}


# --- the spread statistic ---------------------------------------------------------------


def test_a_manager_is_averaged_over_their_seasons_before_being_compared():
    """Otherwise one odd season reads as "these people are different"."""
    steady = History(seasons=(
        season(2022, {1: [40] * 4, 2: [40] * 4, 3: [40] * 4, 4: [40] * 4}),
        season(2023, {1: [40] * 4, 2: [40] * 4, 3: [40] * 4, 4: [40] * 4}),
        season(2024, {1: [40] * 4, 2: [40] * 4, 3: [40] * 4, 4: [40] * 4}),
    ))
    assert between_manager_spread(steady, actual(steady), total_spend) == pytest.approx(0.0)


def test_a_manager_with_too_few_seasons_is_left_out_of_the_spread():
    one = History(seasons=(season(2024, {1: [40] * 4, 2: [40] * 4, 3: [40] * 4, 4: [40] * 4}),))
    assert between_manager_spread(one, actual(one), total_spend, min_seasons=3) == 0.0


# --- planted effects, which is what the harness is for -------------------------------------


def _consistent_history() -> History:
    """The same manager builds the same shape every year.

    Ann always buys one big player, Dee always spreads it thin. That is a real
    per-manager effect and the harness must find it.
    """
    shape = {1: [140, 20, 20, 20], 2: [80, 60, 30, 30], 3: [60, 50, 50, 40],
             4: [50, 50, 50, 50]}
    return History(seasons=tuple(season(y, shape) for y in (2022, 2023, 2024, 2025)))


def _noise_history() -> History:
    """Every manager builds the same roster. There is nothing to find."""
    flat = {t: [50, 50, 50, 50] for t in (1, 2, 3, 4)}
    return History(seasons=tuple(season(y, flat) for y in (2022, 2023, 2024, 2025)))


def test_a_planted_per_manager_effect_survives_the_roster_null():
    ev = permutation(
        _consistent_history(), top_buy, name="top_buy", null="rosters",
        rounds=200, seed=7,
    )
    assert ev.survives, f"planted effect scored only z={ev.z:+.1f}"


def test_pure_noise_does_not_survive():
    ev = permutation(
        _noise_history(), top_buy, name="top_buy", null="rosters", rounds=200, seed=7
    )
    assert not ev.survives


def test_the_wrong_null_invents_variance_the_budget_makes_impossible():
    """The load-bearing test, and the exact shape of the bug that got shipped.

    Every manager here spends all $200, as they very nearly do in reality. So
    the real between-manager spread of total spend is zero, and the roster-level
    null agrees: swapping intact rosters cannot change what anybody spent.

    Shuffle *individual picks* and that constraint evaporates — an imaginary
    manager collects four expensive players and "spends" $400. The null spread
    balloons past anything reality can produce, the z goes sharply negative, and
    a signal that is merely absent starts looking like evidence of the opposite.
    That is how a real pace effect scored −1.7 and was nearly discarded.

    A negative z is therefore not a weak result. It is the harness reporting on
    itself, which is what `suspect` exists to say.
    """
    same_money = {1: [140, 20, 20, 20], 2: [80, 60, 30, 30],
                  3: [60, 50, 50, 40], 4: [50, 50, 50, 50]}  # all total $200
    history = History(seasons=tuple(season(y, same_money) for y in (2022, 2023, 2024, 2025)))

    right = permutation(history, total_spend, name="t", null="rosters", rounds=200, seed=7)
    wrong = permutation(history, total_spend, name="t", null="picks", rounds=200, seed=7)

    # Reality and the correct null agree: nothing to find, and nothing invented.
    assert right.observed == pytest.approx(0.0)
    assert right.mean == pytest.approx(0.0)
    assert right.suspect is False

    # The wrong null manufactures spread out of impossible rosters.
    assert wrong.mean > 0.05
    assert wrong.suspect is True
    assert "check the null" in wrong.verdict


def test_the_same_seed_gives_the_same_answer():
    """A report that cannot be re-derived goes stale while still sounding
    authoritative, so the numbers in it have to be reproducible."""
    history = _consistent_history()
    a = permutation(history, top_buy, name="t", null="rosters", rounds=80, seed=3)
    b = permutation(history, top_buy, name="t", null="rosters", rounds=80, seed=3)

    assert (a.observed, a.mean, a.stdev) == (b.observed, b.mean, b.stdev)


# --- the metrics this codebase actually ships ------------------------------------------


def test_every_shipped_metric_declares_which_null_applies_to_it():
    """A future signal must not silently inherit the wrong one."""
    from ffa.history.evidence import make_metrics

    metrics = make_metrics(_consistent_history())

    assert metrics
    for name, (null, metric) in metrics.items():
        assert null in {"rosters", "picks", "nominators"}, name
        assert callable(metric), name


def test_budget_shaped_metrics_use_the_roster_null():
    """Pace, spend shape, positional allocation and chasing are all constrained
    by the $200 and the fifteen slots."""
    from ffa.history.evidence import make_metrics

    metrics = make_metrics(_consistent_history())
    for name in ("pace", "spend_shape", "te_share", "chasing"):
        assert metrics[name][0] == "rosters", name


def test_nomination_metrics_use_the_nominator_null():
    """You can nominate anybody regardless of what you have left, so nominations
    are not budget-constrained and the roster shuffle would be wrong."""
    from ffa.history.evidence import make_metrics

    metrics = make_metrics(_consistent_history())
    for name in ("nomination_premium", "self_win_rate"):
        assert metrics[name][0] == "nominators", name


def test_pace_measures_share_of_budget_not_share_of_own_spend():
    """During a draft you know a rival's budget and not what they will end up
    spending, so the tested statistic has to be the one the live feature can
    actually compute."""
    from ffa.history.evidence import make_metrics

    history = History(seasons=(season(2024, {1: [100, 0, 0, 0], 2: [25] * 4,
                                             3: [25] * 4, 4: [25] * 4}),))
    pace = make_metrics(history)["pace"][1]
    early = [p for p in history.seasons[0].picks if p.team_id == 1]

    # $100 of a $200 budget, regardless of what team 1 eventually spends.
    assert pace(early, 2024) == pytest.approx(0.5)
