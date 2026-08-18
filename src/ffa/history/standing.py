"""How good is this manager, measured rather than remembered.

The dossier's first substantive question is "how good are they?", and its stated
purpose is to set how much to trust every other read: a sharp manager's odd bid
usually means they know something, a casual one's usually does not. That is the
one archetype where the record answers better than recall, because it is exactly
what a league table is.

**It clears its null at z = +2.9.** Shuffle which manager got which finishing
position inside each season, two thousand times, and the real spread of mean
finishes is well outside the shuffled one — people land in the same part of this
table year after year. (Points-for does *not* clear it, at +1.4, which is why
placing is used and scoring is only shown alongside.)

Two honest limits, both stated wherever this is presented:

- **It measures the season, not the draft.** Injuries, waivers and trades all
  land in a finishing position. Over four seasons that noise averages out enough
  to rank people; it does not make any single finish evidence of anything.
- **`erratic` is a real answer, not a failure to classify.** The dossier's own
  vocabulary has it — "capable of anything, either way" — and somebody who
  finishes 4th, 1st, 12th, 11th is genuinely that, however good their mean looks.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping

from ffa.history.schema import History

# How far from the league's middle a mean finish has to sit before it is worth a
# label. With twelve teams and four seasons, 0.7σ separates the ends without
# labelling the entire middle of the table.
LABEL_Z = 0.7

# Spread of finishes, in places, past which somebody is unpredictable regardless
# of where they average out. Four seasons ranging 1st to 12th is not a "solid"
# manager having bad luck.
ERRATIC_RANGE = 7.0

# Below this there is no record to read.
MIN_SEASONS = 3


@dataclass(frozen=True)
class Standing:
    """One manager's record in the league table."""

    owner_id: str
    finishes: tuple[int, ...] = ()
    points: tuple[float, ...] = ()
    skill: str = ""

    @property
    def seasons(self) -> int:
        return len(self.finishes)

    @property
    def mean_finish(self) -> float:
        return statistics.fmean(self.finishes) if self.finishes else 0.0

    @property
    def swing(self) -> float:
        """Best to worst, in places."""
        return (max(self.finishes) - min(self.finishes)) if self.finishes else 0.0

    @property
    def mean_points(self) -> float:
        return statistics.fmean(self.points) if self.points else 0.0

    def summary(self) -> str:
        places = ", ".join(str(f) for f in self.finishes)
        return (
            f"finished {places} across {self.seasons} seasons "
            f"(mean {self.mean_finish:.1f}, {self.mean_points:.0f} pts/season)"
        )


def collect(history: History) -> dict[str, Standing]:
    """Each manager's finishes, in season order, keyed by resolved owner."""
    finishes: dict[str, list[tuple[int, int]]] = defaultdict(list)
    points: dict[str, list[tuple[int, float]]] = defaultdict(list)

    for season in history.seasons:
        for team_id, owner in season.owners.items():
            if team_id in season.finishes:
                finishes[owner].append((season.year, season.finishes[team_id]))
            if team_id in season.points_for:
                points[owner].append((season.year, season.points_for[team_id]))

    return {
        owner: Standing(
            owner_id=owner,
            finishes=tuple(f for _y, f in sorted(rows)),
            points=tuple(p for _y, p in sorted(points.get(owner, []))),
        )
        for owner, rows in finishes.items()
    }


def classify(standings: Mapping[str, Standing]) -> dict[str, Standing]:
    """Label each manager against this league's own table.

    Relative, like every other classification here: a mean finish of 4.0 means
    something different in a twelve-team league than in an eight-team one, and
    what matters is who is ahead of the people they play.
    """
    from dataclasses import replace

    eligible = [s for s in standings.values() if s.seasons >= MIN_SEASONS]
    if len(eligible) < 2:
        return dict(standings)

    means = [s.mean_finish for s in eligible]
    centre = statistics.fmean(means)
    spread = statistics.pstdev(means)

    out: dict[str, Standing] = {}
    for owner, standing in standings.items():
        if standing.seasons < MIN_SEASONS:
            out[owner] = standing
            continue

        # Checked first: a wide swing is its own answer, whatever the mean says.
        if standing.swing >= ERRATIC_RANGE:
            skill = "erratic"
        elif spread <= 1e-9:
            skill = "solid"
        else:
            # A *lower* finish number is better, so the sign is inverted here.
            z = (centre - standing.mean_finish) / spread
            skill = "sharp" if z >= LABEL_Z else ("casual" if z <= -LABEL_Z else "solid")
        out[owner] = replace(standing, skill=skill)

    return out


def finish_evidence(history: History, *, rounds: int = 2000, seed: int = 0):
    """Do people really land in the same part of the table year after year?

    The null shuffles which manager got which finishing position *within* each
    season, which preserves the fact that every season hands out exactly one of
    each place — the same care the roster-level shuffle takes with a budget.
    Without that constraint the null would be nonsense.
    """
    import random

    from ffa.history.evidence import Evidence

    standings = collect(history)
    by_year: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for owner, standing in standings.items():
        for season, finish in zip(
            [s.year for s in history.seasons if owner in s.owners.values()],
            standing.finishes,
        ):
            by_year[season].append((owner, finish))

    def spread(assignment: Mapping[int, list[tuple[str, int]]]) -> float:
        per: dict[str, list[int]] = defaultdict(list)
        for rows in assignment.values():
            for owner, finish in rows:
                per[owner].append(finish)
        means = [
            statistics.fmean(v) for v in per.values() if len(v) >= MIN_SEASONS
        ]
        return statistics.pstdev(means) if len(means) > 1 else 0.0

    observed = spread(by_year)

    rng = random.Random(seed)
    nulls = []
    for _ in range(rounds):
        shuffled: dict[int, list[tuple[str, int]]] = {}
        for year, rows in by_year.items():
            owners = [o for o, _ in rows]
            places = [f for _, f in rows]
            rng.shuffle(places)
            shuffled[year] = list(zip(owners, places))
        nulls.append(spread(shuffled))

    return Evidence(
        name="finishing position",
        null="seasons",
        observed=observed,
        mean=statistics.fmean(nulls) if nulls else 0.0,
        stdev=statistics.pstdev(nulls) if len(nulls) > 1 else 0.0,
        rounds=rounds,
    )


def standings_for(history: History) -> dict[str, Standing]:
    return classify(collect(history))
