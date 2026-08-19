"""Turn `mSettings` and `mTeam` into typed records.

Everything here is pure — no network, no files — so it can be tested offline
against the committed fixtures, which is the only way any of it was verified
in the first place.

Two facts make reading the raw views necessary rather than merely preferable:

- The popular `espn-api` library's `BaseSettings` drops both `auctionBudget`
  and the draft `type`. Those are the two fields a salary-cap league is
  entirely defined by.
- **Team ids are not contiguous.** The league this was built against runs
  1-5 and 7-13 — there is no team 6, because a team was once removed and
  re-added. `range(1, count + 1)` invents a phantom team *and* drops a real
  one, silently. Ids are always read, never generated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from espn_fantasy.credentials import fold_owner_id
from espn_fantasy.enums import LINEUP_SLOT_IDS, RosterSlot
from espn_fantasy.errors import EspnApiError


@dataclass(frozen=True)
class Member:
    """One ESPN account. Three different names, and they are not interchangeable."""

    member_id: str
    display_name: str = ""   # the account handle: `macurl1392`, `espn06814226`
    real_name: str = ""      # `firstName lastName`: "Michael Curley"


@dataclass(frozen=True)
class TeamDirectory:
    """Who sits where, under every name ESPN knows them by.

    Returned as a record rather than a tuple because the useful thing about it
    is that these are *different* names for the same person, and picking the
    wrong one is invisible. That is not hypothetical: preferring
    `display_name` over `real_name` is what puts `macurl1392` on every surface
    while "Michael Curley" sits in the same payload.
    """

    team_ids: tuple[int, ...] = ()
    owners: dict[int, str] = field(default_factory=dict)
    team_names: dict[int, str] = field(default_factory=dict)
    real_names: dict[int, str] = field(default_factory=dict)
    display_names: dict[int, str] = field(default_factory=dict)

    def name_for(self, team_id: int) -> str:
        """The best human label available, in the order a person would want it."""
        return (
            self.real_names.get(team_id)
            or self.display_names.get(team_id)
            or self.team_names.get(team_id)
            or f"team{team_id}"
        )


@dataclass(frozen=True)
class LeagueSettings:
    """What ESPN says this league is.

    A faithful record of the payload, not an interpretation of it: no defaults
    are invented for fields ESPN did not send, because a plausible default is
    indistinguishable from a real value once it is one layer downstream.
    """

    league_id: int
    year: int
    name: str = ""

    draft_type: str = ""
    auction_budget: int | None = None
    keeper_count: int | None = None
    scoring_type: str = ""

    team_count: int = 0
    roster: Mapping[RosterSlot, int] = field(default_factory=dict)

    teams: TeamDirectory = field(default_factory=TeamDirectory)

    @property
    def is_auction(self) -> bool:
        return self.draft_type.upper() == "AUCTION"

    @property
    def roster_size(self) -> int:
        return sum(self.roster.values())

    @property
    def starter_count(self) -> int:
        return sum(n for slot, n in self.roster.items() if slot.is_starter)


def roster_from_lineup_counts(
    counts: Mapping[str, Any]
) -> tuple[dict[RosterSlot, int], list[str]]:
    """`{"0": 1, "2": 2, ...}` -> `{QB: 1, RB: 2, ...}`, plus any warnings.

    Zero-count slots are dropped: ESPN sends every slot id it knows about, and
    keeping the zeros puts empty QB/DT/LB entries into the result.

    An unmapped slot with a real count is *reported*, never silently skipped —
    those are roster spots that exist in the league and would otherwise be
    missing from every count derived from this.
    """
    roster: dict[RosterSlot, int] = {}
    warnings: list[str] = []

    for raw_id, raw_count in (counts or {}).items():
        try:
            slot_id, count = int(raw_id), int(raw_count)
        except (TypeError, ValueError):
            warnings.append(f"skipped unreadable lineup slot {raw_id!r}: {raw_count!r}")
            continue
        if count <= 0:
            continue

        slot = LINEUP_SLOT_IDS.get(slot_id)
        if slot is None:
            warnings.append(
                f"lineup slot id {slot_id} (count {count}) has no mapping — "
                "those roster spots are missing from any count derived from this"
            )
            continue
        roster[slot] = roster.get(slot, 0) + count

    return roster, warnings


def _members_by_id(payload: Mapping[str, Any]) -> dict[str, Member]:
    """Every member, keeping both names rather than choosing between them."""
    out: dict[str, Member] = {}
    for member in payload.get("members") or []:
        member_id = member.get("id")
        if not member_id:
            continue
        real = " ".join(
            str(p).strip()
            for p in (member.get("firstName"), member.get("lastName"))
            if p and str(p).strip()
        )
        out[str(member_id)] = Member(
            member_id=str(member_id),
            display_name=str(member.get("displayName") or "").strip(),
            real_name=real,
        )
    return out


def teams_from_payload(payload: Mapping[str, Any]) -> TeamDirectory:
    """Who holds each seat, and what they are called.

    Ids come back sorted and exactly as ESPN reported them.
    """
    members = _members_by_id(payload)
    team_ids: list[int] = []
    owners: dict[int, str] = {}
    names: dict[int, str] = {}
    real_names: dict[int, str] = {}
    display_names: dict[int, str] = {}

    for team in payload.get("teams") or []:
        try:
            team_id = int(team["id"])
        except (KeyError, TypeError, ValueError):
            continue
        team_ids.append(team_id)

        name = (team.get("name") or "").strip()
        if name:
            names[team_id] = name

        owner = team.get("primaryOwner")
        if owner:
            owners[team_id] = str(owner)
            member = members.get(str(owner))
            if member is not None:
                if member.real_name:
                    real_names[team_id] = member.real_name
                if member.display_name:
                    display_names[team_id] = member.display_name

    return TeamDirectory(
        team_ids=tuple(sorted(team_ids)),
        owners=owners,
        team_names=names,
        real_names=real_names,
        display_names=display_names,
    )


def my_team_id(owners: Mapping[int, str], swid: str | None) -> int:
    """Which team belongs to a SWID. `0` when it cannot be determined.

    Returning 0 rather than guessing is the whole point: a wrong answer here
    is worse than no answer, because everything keyed off "my team" then
    describes somebody else's roster with full confidence.

    Both sides are folded — braces stripped, case ignored — since a SWID
    copied out of a browser rarely matches `primaryOwner` byte for byte.
    """
    wanted = fold_owner_id(swid)
    if not wanted:
        return 0
    for team_id, owner in owners.items():
        if fold_owner_id(owner) == wanted:
            return team_id
    return 0


def settings_from_payloads(
    settings_payload: Mapping[str, Any],
    teams_payload: Mapping[str, Any] | None = None,
    *,
    league_id: int,
    year: int,
) -> tuple[LeagueSettings, list[str]]:
    """Assemble a `LeagueSettings`. Pure — no network, no files.

    Returns the settings and a list of warnings. The warnings are not
    decoration: each one marks a place where the payload was less complete or
    less consistent than it looked, and where a caller reading the record
    alone would draw a confident wrong conclusion.
    """
    settings = settings_payload.get("settings")
    if not isinstance(settings, dict):
        raise EspnApiError(
            "ESPN returned no `settings` block. Request the mSettings view for "
            "this league and season."
        )

    draft = settings.get("draftSettings") or {}
    roster_settings = settings.get("rosterSettings") or {}
    scoring = settings.get("scoringSettings") or {}

    roster, warnings = roster_from_lineup_counts(
        roster_settings.get("lineupSlotCounts") or {}
    )
    if not roster:
        warnings.append(
            "rosterSettings carried no usable lineup slots, so roster size is "
            "unknown — check `lineupSlotCounts` in the raw payload"
        )

    directory = teams_from_payload(teams_payload or {})

    size = settings.get("size")
    team_count = int(size) if size else len(directory.team_ids)
    if directory.team_ids and team_count != len(directory.team_ids):
        warnings.append(
            f"ESPN reports size={team_count} but returned "
            f"{len(directory.team_ids)} teams; using the ids it actually returned"
        )
        team_count = len(directory.team_ids)

    budget = draft.get("auctionBudget")
    keepers = draft.get("keeperCount")

    return (
        LeagueSettings(
            league_id=league_id,
            year=year,
            name=str(settings.get("name") or ""),
            draft_type=str(draft.get("type") or "").upper(),
            auction_budget=int(budget) if isinstance(budget, (int, float)) else None,
            keeper_count=int(keepers) if isinstance(keepers, (int, float)) else None,
            scoring_type=str(scoring.get("scoringType") or ""),
            team_count=team_count,
            roster=roster,
            teams=directory,
        ),
        warnings,
    )


def fetch_settings(
    client: Any, *, swid: str | None = None
) -> tuple[LeagueSettings, list[str]]:
    """Fetch both views and assemble them. The one impure function here.

    `swid` is optional and only used to append a warning when it matches no
    team — the settings record itself never carries "which team is mine",
    because that is a fact about the caller, not about the league.
    """
    settings, warnings = settings_from_payloads(
        client.settings(),
        client.teams(),
        league_id=client.league_id,
        year=client.year,
    )
    if swid and not my_team_id(settings.teams.owners, swid):
        warnings.append(
            "the SWID given matches none of this league's team owners — check "
            "that it is the account actually in this league"
        )
    return settings, warnings
