"""Config data model.

Everything about the user's league lives here and is loaded from
``config/league.toml``. Nothing in this file is hardcoded to a particular
league — the defaults below exist only so an offline/manual draft can start
without ESPN, and they are all overridable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ffa.domain.enums import Position, RosterSlot


class ConfigError(Exception):
    """Raised for config problems the user must fix by hand.

    The message is shown directly to the user, so it should say what is wrong
    *and* what to do about it.
    """


@dataclass(frozen=True)
class EspnCredentials:
    """ESPN session cookies. Only required for private leagues."""

    espn_s2: str
    swid: str

    def as_cookies(self) -> dict[str, str]:
        # ESPN expects SWID wrapped in braces. Users copying from devtools
        # sometimes strip them, so put them back rather than 401 mysteriously.
        swid = self.swid.strip()
        if swid and not swid.startswith("{"):
            swid = "{" + swid.strip("{}") + "}"
        return {"espn_s2": self.espn_s2.strip(), "SWID": swid}

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let credentials reach a log line or traceback.
        return "EspnCredentials(espn_s2='<redacted>', swid='<redacted>')"


@dataclass(frozen=True)
class LeagueConfig:
    """The shape of the league. Drives every budget and roster calculation."""

    league_id: int
    year: int
    private: bool = True
    name: str = ""

    draft_type: str = "AUCTION"
    budget: int = 200

    team_count: int = 12
    my_team_id: int = 0

    roster: Mapping[RosterSlot, int] = field(
        default_factory=lambda: {
            RosterSlot.QB: 1,
            RosterSlot.RB: 2,
            RosterSlot.WR: 2,
            RosterSlot.TE: 1,
            RosterSlot.FLEX: 1,
            RosterSlot.K: 1,
            RosterSlot.DST: 1,
            RosterSlot.BE: 7,
            RosterSlot.IR: 1,
        }
    )
    flex_positions: tuple[Position, ...] = (Position.RB, Position.WR, Position.TE)

    scoring_type: str = "PPR"

    baseline_teams: int = 12
    baseline_budget: int = 200

    poll_interval_seconds: float = 3.0
    poll_failure_threshold: int = 4

    # --- derived, never stored ---

    @property
    def draftable_slots(self) -> int:
        """Roster spots a team must fill in the auction.

        IR is excluded: you don't draft into it, so it must not inflate the
        "how many players do I still owe money for" count that max-bid uses.
        """
        return sum(n for slot, n in self.roster.items() if slot is not RosterSlot.IR)

    @property
    def starting_slots(self) -> int:
        return sum(n for slot, n in self.roster.items() if slot.is_starter)

    @property
    def total_league_money(self) -> int:
        return self.team_count * self.budget

    @property
    def is_auction(self) -> bool:
        return self.draft_type.upper() == "AUCTION"

    def slots_for_position(self, pos: Position) -> tuple[RosterSlot, ...]:
        """Which roster slots this position can legally occupy, best-fit first."""
        out: list[RosterSlot] = []
        try:
            out.append(RosterSlot(pos.value))
        except ValueError:
            pass
        if pos in self.flex_positions and RosterSlot.FLEX in self.roster:
            out.append(RosterSlot.FLEX)
        if RosterSlot.BE in self.roster:
            out.append(RosterSlot.BE)
        return tuple(out)

    def validate(self) -> None:
        """Fail loudly at startup rather than producing wrong advice at pick 40."""
        problems: list[str] = []

        if self.league_id <= 0:
            problems.append("league.league_id must be a positive integer")
        if not (2000 <= self.year <= 2100):
            problems.append(f"league.year looks wrong: {self.year}")
        if self.budget <= 0:
            problems.append("draft.budget must be positive")
        if self.team_count < 2:
            problems.append("teams.count must be at least 2")
        if self.draftable_slots <= 0:
            problems.append("roster has no draftable slots")
        if self.baseline_teams <= 0 or self.baseline_budget <= 0:
            problems.append("reference.baseline_teams/baseline_budget must be positive")
        if self.poll_interval_seconds < 0.5:
            problems.append(
                "polling.interval_seconds below 0.5 risks rate-limiting from ESPN"
            )
        if self.poll_failure_threshold < 1:
            problems.append("polling.failure_threshold must be at least 1")

        # A budget that can't cover a full roster at $1/player is unfillable.
        if self.budget < self.draftable_slots:
            problems.append(
                f"draft.budget (${self.budget}) is less than the {self.draftable_slots} "
                "draftable roster slots — every team needs at least $1 per slot"
            )

        if not self.is_auction:
            problems.append(
                f"draft.type is {self.draft_type!r}; this tool is built for AUCTION "
                "drafts. Set it to AUCTION or re-run `ffa config init`."
            )

        if problems:
            raise ConfigError(
                "config/league.toml has problems:\n  - " + "\n  - ".join(problems)
            )
