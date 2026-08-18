"""The state a draft is in, and the entities inside it.

Two rules shape everything here:

**Nothing computable is stored.** A team has no `remaining_budget` field and no
roster list. Those are pure functions of the surviving events, computed in
`projections.py`. This kills the entire class of "budget says $87 but the
roster says $92" drift bugs — and it is *why undo works*: undoing a sale
doesn't require patching a team's roster, it just changes which players project
onto that team.

**Everything is a frozen dataclass with no identity-based fields.** That gives
structural `==` for free, which is what lets the resume test assert
`resumed.state == original.state` with a plain equality check.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from ffa.domain.enums import Position, RosterSlot
from ffa.domain.ids import PlayerRef
from ffa.domain.sourced import Sourced


@dataclass(frozen=True)
class LeagueSnapshot:
    """League shape as of draft start, captured in `DraftInitialized`.

    Deliberately a copy rather than a reference to `LeagueConfig`: the journal
    must be self-contained, so that editing `config/league.toml` halfway
    through a draft cannot silently change the max-bid arithmetic when you
    resume.
    """

    league_id: int
    year: int
    name: str
    draft_type: str
    budget: int
    team_count: int
    my_team_id: int
    roster: Mapping[RosterSlot, int]
    flex_positions: tuple[Position, ...]

    # The nomination cycle, one entry per seat, captured here for the same
    # reason every other setting is: who is on the clock must not change because
    # someone edited `config/league.toml` while the draft was running. Empty
    # means unknown, and no reader may substitute id order for it.
    nomination_order: tuple[int, ...] = ()

    @property
    def draftable_slots(self) -> int:
        """Roster spots that must be bought. IR is excluded — you don't draft into it."""
        return sum(n for slot, n in self.roster.items() if slot is not RosterSlot.IR)

    def slots_for_position(self, pos: Position) -> tuple[RosterSlot, ...]:
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

    @property
    def total_nominations(self) -> int:
        """Every nomination the draft will contain, first to last.

        One per roster spot per team: an auction resolves each nomination with a
        sale, so the board and the nomination sequence are the same length.
        """
        return self.team_count * self.draftable_slots

    def nominator_at(self, index: int) -> int | None:
        """Who nominates at 0-based `index`, or `None` if that is unknowable.

        The cycle repeats verbatim every round — measured, not assumed — so this
        is a modulo and nothing more. Returns `None` past the last nomination as
        well as when the order is unknown; a caller must not be handed a
        plausible seat for a turn that does not exist.
        """
        if not self.nomination_order or index < 0:
            return None
        if self.total_nominations and index >= self.total_nominations:
            return None
        return self.nomination_order[index % len(self.nomination_order)]


@dataclass(frozen=True)
class TeamEntity:
    """A team. Note the absence of budget and roster — see the module docstring.

    **Identity anchors on `owner_id` (ESPN's member SWID), never on `name`.**
    Managers rename their teams constantly — mid-season, sometimes mid-draft —
    so a team name is display text with a short shelf life, not a key. The
    owner id is what actually persists.
    """

    team_id: int
    owner_id: Sourced[str] | None = None
    manager: Sourced[str] | None = None
    name: Sourced[str] | None = None

    @property
    def label(self) -> str:
        """What to call this team in output.

        Deliberately never falls through to `name`: a readout that silently
        starts calling a manager by whatever they just renamed their team to is
        worse than one that says `team11`. Set nicknames in [managers].
        """
        if self.manager is not None and self.manager.is_known:
            return str(self.manager.value)
        return f"team{self.team_id}"


@dataclass(frozen=True)
class PlayerEntity:
    """A player someone has nominated, bought, or amended.

    `sold` and `nominated` are plain bools, not `Sourced`: they are facts about
    which events survived replay, not observations to be merged.
    """

    ref: PlayerRef
    team: Sourced[int] | None = None
    price: Sourced[int] | None = None
    position: Sourced[Position] | None = None
    sold: bool = False
    nominated: bool = False

    @property
    def is_dangling(self) -> bool:
        """Amended but not sold — normally the result of undoing a sale.

        These render under their own heading and charge nobody, which is the
        legible outcome of undo deliberately not cascading into later
        amendments.
        """
        return not self.sold and (self.price is not None or self.team is not None)


@dataclass(frozen=True)
class NominationEntity:
    event_id: int
    ref: PlayerRef
    nominated_by: Sourced[int] | None = None
    opening_bid: Sourced[int] | None = None


@dataclass(frozen=True)
class DraftState:
    draft_id: str
    league: LeagueSnapshot | None = None
    teams: Mapping[int, TeamEntity] = field(default_factory=dict)
    players: Mapping[str, PlayerEntity] = field(default_factory=dict)
    current_nomination: NominationEntity | None = None
    applied_ids: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def empty(cls, draft_id: str = "") -> "DraftState":
        return cls(draft_id=draft_id)

    @property
    def is_initialized(self) -> bool:
        return self.league is not None

    def sold_players(self) -> tuple[PlayerEntity, ...]:
        # Sorted for deterministic output — dict order must never leak into a
        # rendered readout or a projection.
        return tuple(sorted((p for p in self.players.values() if p.sold), key=lambda p: p.ref.key))

    def players_for_team(self, team_id: int) -> tuple[PlayerEntity, ...]:
        return tuple(
            p
            for p in self.sold_players()
            if p.team is not None and p.team.value == team_id
        )

    def with_warning(self, message: str) -> "DraftState":
        """Anomalies accumulate here rather than raising.

        Reducers are total: an event is already durably on disk by the time it
        is applied, so refusing it would make journal and state disagree.
        """
        return replace(self, warnings=self.warnings + (message,))
