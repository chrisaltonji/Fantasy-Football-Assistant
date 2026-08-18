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

    # ESPN team ids are NOT contiguous. A league that has ever removed and
    # re-added a team leaves a hole — the real league this was built against
    # runs 1-5 and 7-13, with no team 6. Assuming range(1, count+1) invents a
    # phantom team and silently drops a real one, so the ids are explicit.
    # Empty falls back to 1..team_count, which is right for untouched leagues.
    team_ids: tuple[int, ...] = ()

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

    # Manager nicknames keyed by ESPN team id. This is what you actually type
    # mid-draft (`sold barkley 62 dave`), so short beats formal. Teams without
    # a nickname stay addressable as `t3`/`team3`/`3`.
    managers: Mapping[int, str] = field(default_factory=dict)

    # ESPN member SWIDs keyed by team id. The stable anchor for who a team
    # actually is — team names change constantly, so nothing keys off them.
    # Lets CP5 detect a team-id shift instead of silently attributing picks to
    # the wrong manager.
    owners: Mapping[int, str] = field(default_factory=dict)

    # Second accounts. `{secondary SWID} = "{primary SWID}"`, both folded.
    #
    # A manager who drafts under two ESPN logins is two people to every join in
    # this codebase, and the arithmetic stays perfectly correct about somebody
    # who does not exist. This league has two cases: Brian Cona held team 6
    # through 2023 under one account and returned at team 13 under another, and
    # there is an orphan second login with no seat at all.
    #
    # Declared by hand and never inferred — two similar names are not evidence,
    # and guessing at identity is what `resolve_team` and `PlayerBook` both
    # refuse to do. See `ffa.config.identity`.
    aliases: Mapping[str, str] = field(default_factory=dict)

    # The manager's actual name, keyed by team id — "Michael Curley". ESPN
    # carries this in `mTeam`'s `members[].firstName/lastName`, alongside the
    # account handle, and for a long time this codebase read only the handle.
    #
    # Display only, and never a nickname: it has a space in it, and the command
    # grammar splits on whitespace, so `sold barkley 62 michael curley` would
    # parse `curley` as a separate token. `managers` holds the typeable form.
    real_names: Mapping[int, str] = field(default_factory=dict)

    # Team names keyed by team id. A **matching hint, not identity** — the one
    # place a name is unavoidable. ESPN's draft room board carries no team id
    # anywhere in its DOM, and its column order is draft order rather than id
    # order (the real league's team 3 sits in column 1), so the name is the
    # only join between a board column and a team id. A rename mid-draft breaks
    # the match, which is why the reader falls back and says so loudly rather
    # than guessing.
    team_names: Mapping[int, str] = field(default_factory=dict)

    # Where your exported auction values live. Unset falls back to the sample
    # fixture, with a loud banner — sample values are invented.
    reference_path: str = ""

    baseline_teams: int = 12
    baseline_budget: int = 200

    poll_interval_seconds: float = 3.0
    poll_failure_threshold: int = 4

    # --- derived, never stored ---

    @property
    def effective_team_ids(self) -> tuple[int, ...]:
        """The league's actual team ids, in order."""
        return self.team_ids or tuple(range(1, self.team_count + 1))

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

        ids = self.effective_team_ids
        if self.team_ids:
            if len(set(self.team_ids)) != len(self.team_ids):
                problems.append("[teams].ids contains duplicates")
            if len(self.team_ids) != self.team_count:
                problems.append(
                    f"[teams].ids has {len(self.team_ids)} entries but teams.count "
                    f"is {self.team_count} — they must agree"
                )
            if any(i <= 0 for i in self.team_ids):
                problems.append("[teams].ids must all be positive")

        if self.my_team_id and self.my_team_id not in ids:
            problems.append(
                f"teams.my_team_id is {self.my_team_id}, which is not one of the "
                f"league's team ids: {', '.join(str(i) for i in ids)}"
            )

        for team_id, nickname in self.managers.items():
            if team_id not in ids:
                problems.append(
                    f"[managers] names team {team_id}, which is not in this league. "
                    f"Valid ids: {', '.join(str(i) for i in ids)}"
                )
            if not str(nickname).strip():
                problems.append(f"[managers] entry for team {team_id} is empty")

        duplicates = {
            name.lower() for name in self.managers.values()
            if list(n.lower() for n in self.managers.values()).count(name.lower()) > 1
        }
        if duplicates:
            problems.append(
                "[managers] nicknames must be unique so they can be typed "
                f"unambiguously; repeated: {', '.join(sorted(duplicates))}"
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
