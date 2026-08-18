"""The closed set of things that can happen in a draft.

Five types, deliberately. Each addition is a permanent commitment — journals
written today must replay under every future version — so the bar for a sixth
is high. Things considered and rejected: `DraftClosed` (state can compute "all
slots filled"), `NominationCancelled` (undo covers it), `TeamsIdentified`
(`FieldAmended` covers it), `EventRedone` (undoing an `EventUndone` is redo).

The wrapping rule throughout: **identity fields are plain, observed attributes
are `Sourced`.** Which player a sale is about is not an observation to be
merged — it's the key merge works against. The price is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from ffa.domain.enums import Position, RosterSlot
from ffa.domain.ids import PlayerRef
from ffa.domain.models import LeagueSnapshot
from ffa.domain.sourced import Sourced

SCHEMA_VERSION = 1


@dataclass(frozen=True, kw_only=True)
class BaseEvent:
    """Envelope shared by every event.

    `id` is a dense 1-based integer assigned by the journal at append time, not
    a UUID — `undo #7` has to be typeable under time pressure, and the
    single-writer discipline already guarantees uniqueness.

    `at` is when the writer *recorded* the event, and is deliberately distinct
    from each `Sourced.observed_at`: a poll may observe a sale at T and record
    it at T+3s, and merge must reason about observation time, not write time.
    """

    id: int = 0
    at: datetime | None = None
    source: str = "MANUAL"
    note: str | None = None

    def to_payload(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    @classmethod
    def from_payload(cls, obj: Mapping[str, Any]) -> "BaseEvent":  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class TeamSeed:
    """A team as known at draft start.

    `owner_id` is ESPN's member SWID and is the stable anchor; `name` is
    volatile display text that managers change on a whim.
    """

    team_id: int
    owner_id: str = ""
    manager: str = ""
    name: str = ""


@dataclass(frozen=True, kw_only=True)
class DraftInitialized(BaseEvent):
    """Always line 1. Carries the full league shape.

    Copying league settings into the journal rather than re-reading
    `config/league.toml` on resume is what stops an edit mid-draft from
    silently changing every budget calculation.
    """

    draft_id: str
    league: LeagueSnapshot
    teams: tuple[TeamSeed, ...] = ()
    schema_version: int = SCHEMA_VERSION
    app_version: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "league": {
                "league_id": self.league.league_id,
                "year": self.league.year,
                "name": self.league.name,
                "draft_type": self.league.draft_type,
                "budget": self.league.budget,
                "team_count": self.league.team_count,
                "my_team_id": self.league.my_team_id,
                "roster": {slot.value: n for slot, n in self.league.roster.items()},
                "flex_positions": [p.value for p in self.league.flex_positions],
                "nomination_order": list(self.league.nomination_order),
            },
            "teams": [
                {
                    "team_id": t.team_id, "owner_id": t.owner_id,
                    "manager": t.manager, "name": t.name,
                }
                for t in self.teams
            ],
        }

    @classmethod
    def from_payload(cls, obj: Mapping[str, Any]) -> "DraftInitialized":
        raw = obj["league"]
        league = LeagueSnapshot(
            league_id=raw["league_id"],
            year=raw["year"],
            name=raw.get("name", ""),
            draft_type=raw.get("draft_type", "AUCTION"),
            budget=raw["budget"],
            team_count=raw["team_count"],
            my_team_id=raw["my_team_id"],
            roster={RosterSlot(k): v for k, v in raw["roster"].items()},
            flex_positions=tuple(Position.parse(p) for p in raw.get("flex_positions", [])),
            # Absent in every journal written before the nomination readout
            # existed. It reads back as unknown, which is the one honest answer
            # for a draft that never recorded an order.
            nomination_order=tuple(raw.get("nomination_order", ()) or ()),
        )
        return cls(
            draft_id=obj["draft_id"],
            league=league,
            teams=tuple(
                TeamSeed(
                    team_id=t["team_id"], owner_id=t.get("owner_id", ""),
                    manager=t.get("manager", ""), name=t.get("name", ""),
                )
                for t in obj.get("teams", [])
            ),
            schema_version=obj.get("schema_version", SCHEMA_VERSION),
            app_version=obj.get("app_version", ""),
        )


@dataclass(frozen=True, kw_only=True)
class PlayerNominated(BaseEvent):
    player: PlayerRef
    nominated_by: Sourced[int] | None = None
    opening_bid: Sourced[int] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "player": self.player.to_obj(),
            "nominated_by": self.nominated_by.to_obj() if self.nominated_by else None,
            "opening_bid": self.opening_bid.to_obj() if self.opening_bid else None,
        }

    @classmethod
    def from_payload(cls, obj: Mapping[str, Any]) -> "PlayerNominated":
        return cls(
            player=PlayerRef.from_obj(obj["player"]),
            nominated_by=_sourced(obj.get("nominated_by"), int),
            opening_bid=_sourced(obj.get("opening_bid"), int),
        )


@dataclass(frozen=True, kw_only=True)
class PlayerSold(BaseEvent):
    """The core event.

    `price` is always present, even when unknown — it serializes as a `Sourced`
    with an explicit null. An omitted key would be indistinguishable from an
    older schema that lacked the field; an explicit null says "a sale happened
    and we do not know the price", which is a fact rather than an absence.
    """

    player: PlayerRef
    team: Sourced[int]
    price: Sourced[int]
    position: Sourced[Position] | None = None
    nomination_id: int | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "player": self.player.to_obj(),
            "team": self.team.to_obj(),
            "price": self.price.to_obj(),
            "nomination_id": self.nomination_id,
        }
        if self.position is not None:
            payload["position"] = self.position.to_obj()
        return payload

    @classmethod
    def from_payload(cls, obj: Mapping[str, Any]) -> "PlayerSold":
        return cls(
            player=PlayerRef.from_obj(obj["player"]),
            team=Sourced.from_obj(obj["team"], int),
            price=Sourced.from_obj(obj["price"], int),
            position=_sourced(obj.get("position"), Position.parse),
            nomination_id=obj.get("nomination_id"),
        )


@dataclass(frozen=True, kw_only=True)
class FieldAmended(BaseEvent):
    """A correction, and the general mechanism behind several features.

    Targets an **entity, never an event id**. That single decision is what
    makes undo tractable: each amendment is an independent observation about
    the world, so replay is "re-merge whatever survived" rather than walking a
    dependency graph.
    """

    entity: str
    field_name: str
    value: Sourced[Any]

    def to_payload(self) -> dict[str, Any]:
        return {"entity": self.entity, "field": self.field_name, "value": self.value.to_obj()}

    @classmethod
    def from_payload(cls, obj: Mapping[str, Any]) -> "FieldAmended":
        name = obj["field"]
        return cls(
            entity=obj["entity"],
            field_name=name,
            value=Sourced.from_obj(obj["value"], FIELD_DECODERS.get(name)),
        )


@dataclass(frozen=True, kw_only=True)
class EventUndone(BaseEvent):
    """Undo. Applying this is a no-op — undo lives in the replay filter."""

    target_id: int
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"target_id": self.target_id, "reason": self.reason}

    @classmethod
    def from_payload(cls, obj: Mapping[str, Any]) -> "EventUndone":
        return cls(target_id=obj["target_id"], reason=obj.get("reason"))


@dataclass(frozen=True, kw_only=True)
class UnknownEvent(BaseEvent):
    """A type this version doesn't recognize.

    Reading a journal written by a newer `ffa` degrades to "some picks are
    missing" rather than crashing — the right trade for a tool whose failure
    mode is happening live during a draft. The store counts these and the REPL
    prints a loud banner, because silent incompleteness would be worse than
    the crash.
    """

    type_name: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return dict(self.raw)

    @classmethod
    def from_payload(cls, obj: Mapping[str, Any]) -> "UnknownEvent":  # pragma: no cover
        return cls(type_name="", raw=dict(obj))


def _sourced(obj: Any, decode: Callable[[Any], Any] | None) -> Sourced[Any] | None:
    return None if obj is None else Sourced.from_obj(obj, decode)


# Which `Sourced` value type each amendable field carries. Also the whitelist:
# an amendment naming a field absent from this table is rejected at parse time,
# so a typo can't silently write somewhere nothing reads.
FIELD_DECODERS: dict[str, Callable[[Any], Any]] = {
    "price": int,
    "team": int,
    "position": Position.parse,
    "name": str,
    "manager": str,
    "owner_id": str,
    "player_id": str,
}

AMENDABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "player": ("price", "team", "position", "name", "player_id"),
    # `draft` is deliberately absent: changing the budget mid-draft would
    # invalidate every calculation already made against it.
    "team": ("manager", "owner_id", "name"),
}

# Explicit registry rather than metaclass magic or `__init_subclass__`, so it
# is greppable. A test walks the union asserting every class appears here,
# which makes "added an event, forgot the codec" impossible.
EVENT_TYPES: dict[str, type[BaseEvent]] = {
    "DraftInitialized": DraftInitialized,
    "PlayerNominated": PlayerNominated,
    "PlayerSold": PlayerSold,
    "FieldAmended": FieldAmended,
    "EventUndone": EventUndone,
}

Event = BaseEvent
