"""What a past draft looks like once it is off the wire.

Everything here is a plain frozen record with no ESPN in it, for the same reason
the rest of the codebase works that way: the analysis is the part that can be
wrong, and it should be testable without a network.

Two things carry through from the live path and are not up for renegotiation:

- **Managers are anchored on the owner SWID, never the team id.** Team ids look
  stable in this league — but Brian Cona held team 6 through 2023 and left, and
  B C arrived at team 13 in 2024. A profile keyed on the seat would blend two
  people the first time a league churns, and produce a confident read about
  somebody who was never there.
- **A missing value is missing, not zero.** A pick with no reference price is
  excluded from the over-pay maths and counted, rather than treated as a $0
  consensus, which would make every buy look like a chase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class DraftPick:
    """One completed auction purchase."""

    overall_pick: int
    team_id: int
    player_id: int
    price: int
    player_name: str = ""
    position: str = ""
    pro_team: str = ""
    nominating_team_id: int | None = None
    is_keeper: bool = False
    auto_drafted: bool = False

    # What the market said this player was worth that season, already scaled to
    # the league's actual money. `None` means no reference existed for him.
    reference: float | None = None

    @property
    def delta(self) -> float | None:
        return None if self.reference is None else self.price - self.reference

    @property
    def overpay_ratio(self) -> float | None:
        """Price as a multiple of reference. `None` when there is no reference."""
        if self.reference is None or self.reference <= 0:
            return None
        return self.price / self.reference

    @property
    def self_nominated(self) -> bool:
        return (
            self.nominating_team_id is not None
            and self.nominating_team_id == self.team_id
        )


@dataclass(frozen=True)
class SeasonDraft:
    """One season's auction, plus how much we actually know about it."""

    year: int
    league_id: int
    team_count: int
    budget: int
    picks: tuple[DraftPick, ...] = ()

    # team_id -> owner SWID, for that season. The join that makes a manager a
    # person rather than a chair.
    owners: Mapping[int, str] = field(default_factory=dict)
    team_names: Mapping[int, str] = field(default_factory=dict)

    # Where the reference prices came from, and how far they reached. Printed
    # verbatim in the report: a metric computed off 160 of 180 players is a
    # different claim from one computed off all of them.
    reference_source: str = ""
    reference_coverage: float = 0.0

    @property
    def total_spend(self) -> int:
        return sum(p.price for p in self.picks)

    @property
    def has_nominations(self) -> bool:
        """False for a draft ESPN recorded without nominating team ids."""
        return any(p.nominating_team_id for p in self.picks)

    def thirds(self) -> tuple[int, int]:
        """Pick numbers that split the draft into equal thirds."""
        n = max((p.overall_pick for p in self.picks), default=0)
        return (n // 3, (2 * n) // 3)


@dataclass(frozen=True)
class SeasonGap:
    """A season we could not use, and exactly why.

    Recorded rather than skipped. "No data for 2021" and "2021 was a 10-team
    offline draft with no prices" support very different conclusions, and only
    one of them is true.
    """

    year: int
    reason: str


@dataclass(frozen=True)
class History:
    """Every season we could read, and every one we could not."""

    seasons: tuple[SeasonDraft, ...] = ()
    gaps: tuple[SeasonGap, ...] = ()
    league_name: str = ""
    league_id: int = 0

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(s.year for s in self.seasons)
