"""Does a signal survive, or is it what chance looks like?

Four seasons of twelve managers is 48 manager-seasons and about 15 picks each.
That is enough data to rank people and nowhere near enough to stop a plausible
pattern appearing on its own — and a draft assistant that is *confidently* wrong
about a rival is worse than one that says nothing, because it gets acted on at
the moment there is no time to check.

So every claim is scored against a null: shuffle the thing that is supposed to
carry the signal, many times, and ask whether the real spread is wider than the
shuffled one. `z` is how many null standard deviations the observed value sits
above the shuffled mean.

**The null has to respect the constraints of whatever generated the data**, and
getting that wrong is the trap this module exists to close:

- `shuffle_picks` reassigns individual picks between managers. Correct for
  anything not tied to a budget — NFL-team affinity, repeat buys, who nominated
  what.
- `shuffle_rosters` reassigns *whole rosters*. Necessary for anything shaped by
  the $200 and the fifteen slots, because pick-level shuffling lets an imaginary
  manager hold three $70 players. Under the wrong null, pace scored **z = −1.7**
  — an observed spread *narrower* than random, which is impossible for a real
  effect and is the signature of a null more permissive than reality.

That is why `Evidence.suspect` exists. A negative z is not a weak result; it is
a broken test, and it should be read as one.
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from ffa.history.schema import DraftPick, History

# Enough rounds that the null's own standard error is small relative to the
# effects being measured, and few enough to run in a couple of seconds.
ROUNDS = 400

# Two standard deviations above the shuffled mean. Deliberately blunt: the
# question is "is this distinguishable from chance", not "what is the p-value".
REAL_Z = 2.0


@dataclass(frozen=True)
class Evidence:
    """One signal, scored against its null."""

    name: str
    null: str
    observed: float
    mean: float
    stdev: float
    rounds: int = ROUNDS

    @property
    def z(self) -> float:
        return (self.observed - self.mean) / self.stdev if self.stdev else 0.0

    @property
    def survives(self) -> bool:
        return self.z >= REAL_Z

    @property
    def suspect(self) -> bool:
        """An observed spread far *narrower* than random.

        Under a correct null with no effect, z is roughly standard normal, so
        mildly negative values are ordinary. This threshold is for the case that
        cannot happen by accident: a real effect can be absent, but it cannot
        make a population markedly more uniform than shuffling it does. When
        this fires, the null is almost certainly more permissive than the
        process that produced the data — which is exactly how the pace
        mis-specification announced itself at z = −1.7 before the roster-level
        null moved it to +3.7.
        """
        return self.z <= -2.0

    @property
    def verdict(self) -> str:
        if self.suspect:
            return "check the null — spread is narrower than chance"
        if self.survives:
            return "real"
        return "not distinguishable from chance"


# --- assignments -----------------------------------------------------------------


Assignment = Mapping[tuple[int, int], str]


def actual(history: History) -> Assignment:
    """Who really owned each pick. `(year, index) -> owner`."""
    return {
        (season.year, i): season.owners.get(pick.team_id, str(pick.team_id))
        for season in history.seasons
        for i, pick in enumerate(season.picks)
    }


def actual_nominators(history: History) -> Assignment:
    return {
        (season.year, i): (
            season.owners.get(pick.nominating_team_id or -1, "")
            if pick.nominating_team_id
            else ""
        )
        for season in history.seasons
        for i, pick in enumerate(season.picks)
    }


def shuffle_picks(history: History, rng: random.Random, of=actual) -> Assignment:
    """Reassign individual picks within each season.

    Valid where the statistic does not depend on a roster hanging together —
    which NFL team a player comes from, whether a manager has owned him before,
    who put him up for bid.
    """
    out: dict[tuple[int, int], str] = {}
    base = of(history)
    for season in history.seasons:
        owners = [base[(season.year, i)] for i in range(len(season.picks))]
        rng.shuffle(owners)
        for i, owner in enumerate(owners):
            out[(season.year, i)] = owner
    return out


def shuffle_rosters(history: History, rng: random.Random) -> Assignment:
    """Reassign *whole rosters* between managers, within each season.

    Preserves the $200 and the fifteen slots — every shuffled manager still
    holds a roster somebody actually built — and breaks only the link between a
    person and the shape they built across seasons. That is the real question:
    does the same person build a similar roster year after year?
    """
    out: dict[tuple[int, int], str] = {}
    base = actual(history)
    for season in history.seasons:
        by_owner: dict[str, list[int]] = defaultdict(list)
        for i in range(len(season.picks)):
            by_owner[base[(season.year, i)]].append(i)
        owners = list(by_owner)
        shuffled = list(owners)
        rng.shuffle(shuffled)
        for source, target in zip(owners, shuffled):
            for i in by_owner[source]:
                out[(season.year, i)] = target
    return out


# --- the test ---------------------------------------------------------------------


def between_manager_spread(
    history: History,
    assignment: Assignment,
    metric: "Callable[[Sequence[DraftPick], int], float | None]",
    *,
    min_seasons: int = 3,
) -> float:
    """How much managers differ, once each is averaged over their own seasons.

    Averaging first is what separates "these people are different" from "one
    season was odd" — a manager whose value swings wildly year to year
    contributes their mean, not their noise.
    """
    per_season: dict[tuple[str, int], list[DraftPick]] = defaultdict(list)
    for season in history.seasons:
        for i, pick in enumerate(season.picks):
            owner = assignment.get((season.year, i))
            if owner:
                per_season[(owner, season.year)].append(pick)

    per_owner: dict[str, list[float]] = defaultdict(list)
    for (owner, year), picks in per_season.items():
        value = metric(picks, year)
        if value is not None:
            per_owner[owner].append(value)

    means = [
        statistics.fmean(values)
        for values in per_owner.values()
        if len(values) >= min_seasons
    ]
    return statistics.pstdev(means) if len(means) > 1 else 0.0


def permutation(
    history: History,
    metric: "Callable[[Sequence[DraftPick], int], float | None]",
    *,
    name: str,
    null: str = "rosters",
    rounds: int = ROUNDS,
    seed: int = 0,
    min_seasons: int = 3,
) -> Evidence:
    """Score one metric against its null. Seeded, so a report is reproducible.

    `null` names both the shuffle and what is being grouped:

    - `rosters` — whole rosters swap between managers. For anything the $200 and
      the fifteen slots constrain.
    - `picks` — individual picks swap. For anything they do not.
    - `nominators` — individual *nominations* swap. The statistic is about who
      put a player up, which is not budget-constrained at all: you can nominate
      anybody regardless of what you have left.
    """
    base = actual_nominators if null == "nominators" else actual

    if null == "rosters":
        def shuffler(h, rng):
            return shuffle_rosters(h, rng)
    else:
        def shuffler(h, rng):
            return shuffle_picks(h, rng, of=base)

    observed = between_manager_spread(
        history, base(history), metric, min_seasons=min_seasons
    )

    rng = random.Random(seed)
    spreads = [
        between_manager_spread(
            history, shuffler(history, rng), metric, min_seasons=min_seasons
        )
        for _ in range(rounds)
    ]
    return Evidence(
        name=name,
        null=null,
        observed=observed,
        mean=statistics.fmean(spreads) if spreads else 0.0,
        stdev=statistics.pstdev(spreads) if len(spreads) > 1 else 0.0,
        rounds=rounds,
    )


# --- the metrics this codebase actually claims -------------------------------------


PACE_FRACTION = 0.3


def make_metrics(history: History, budget: int = 200) -> dict:
    """The metric callables, bound to this history's own board lengths."""
    board = {
        season.year: max((p.overall_pick for p in season.picks), default=0)
        for season in history.seasons
    }

    def pace(picks: Sequence[DraftPick], year: int) -> float | None:
        """Share of *budget* spent by the 30% mark.

        Budget, not own-spend. During a draft you know a rival's budget and not
        what they will eventually spend, so the tested statistic has to be the
        one the live feature can actually compute. (In this league they differ
        by at most 6% — everybody spends $188-200 of $200 — but the live
        version is the one that has to be right.)

        Board length is per season: 150 picks in a ten-team year, 180 in a
        twelve-team one, so progress is a fraction and never a pick number.
        """
        if not picks:
            return None
        cut = board.get(year, 0) * PACE_FRACTION
        return sum(p.price for p in picks if p.overall_pick <= cut) / budget

    def spend_shape(picks: Sequence[DraftPick], _year: int) -> float | None:
        if not picks:
            return None
        return sum(sorted((p.price for p in picks), reverse=True)[:3]) / budget

    def position_share(position: str):
        def metric(picks: Sequence[DraftPick], _year: int) -> float | None:
            total = sum(p.price for p in picks)
            if not total:
                return None
            return sum(p.price for p in picks if p.position == position) / total

        return metric

    def chasing(picks: Sequence[DraftPick], _year: int) -> float | None:
        referenced = [p for p in picks if p.reference is not None]
        if len(referenced) < 6:
            return None
        total = sum(p.reference for p in referenced)
        return (sum(p.price for p in referenced) / total) if total else None

    # The going rate in each third of each season, so a nomination is judged
    # against what the room was paying at that moment. A $40 nomination means
    # something completely different at pick 8 and at pick 150.
    stages: dict[int, list[float]] = {}
    for season in history.seasons:
        low, high = season.thirds()
        buckets: list[list[int]] = [[], [], []]
        for pick in season.picks:
            index = 0 if pick.overall_pick <= low else (1 if pick.overall_pick <= high else 2)
            buckets[index].append(pick.price)
        stages[season.year] = [
            statistics.median(b) if b else 0.0 for b in buckets
        ] + [low, high]

    def nomination_premium(picks: Sequence[DraftPick], year: int) -> float | None:
        """What their nominations cost, against the going rate at that stage.

        Grouped by *nominator*, not owner — the picks handed to this metric are
        the ones this manager put up, whoever ended up buying them.
        """
        stage = stages.get(year)
        if not stage or len(picks) < 5:
            return None
        medians, low, high = stage[:3], stage[3], stage[4]
        deltas = [
            p.price - medians[0 if p.overall_pick <= low else (1 if p.overall_pick <= high else 2)]
            for p in picks
        ]
        return statistics.fmean(deltas) if deltas else None

    def self_win_rate(picks: Sequence[DraftPick], _year: int) -> float | None:
        """How often they win what they nominate. Also grouped by nominator."""
        if len(picks) < 5:
            return None
        return sum(1 for p in picks if p.self_nominated) / len(picks)

    return {
        "pace": ("rosters", pace),
        "spend_shape": ("rosters", spend_shape),
        "te_share": ("rosters", position_share("TE")),
        "rb_share": ("rosters", position_share("RB")),
        "wr_share": ("rosters", position_share("WR")),
        "qb_share": ("rosters", position_share("QB")),
        "chasing": ("rosters", chasing),
        "nomination_premium": ("nominators", nomination_premium),
        "self_win_rate": ("nominators", self_win_rate),
    }


def assess(history: History, *, rounds: int = ROUNDS, seed: int = 0) -> dict[str, Evidence]:
    """Score every claim this codebase makes. Seeded and reproducible."""
    return {
        name: permutation(
            history, metric, name=name, null=null, rounds=rounds, seed=seed
        )
        for name, (null, metric) in make_metrics(history).items()
    }
