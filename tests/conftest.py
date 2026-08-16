from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ffa.domain.enums import Position, Provenance, RosterSlot
from ffa.domain.events import DraftInitialized, PlayerSold, TeamSeed
from ffa.domain.ids import PlayerRef
from ffa.domain.models import LeagueSnapshot
from ffa.domain.sourced import known, unknown

T0 = datetime(2026, 8, 16, 19, 0, 0, tzinfo=timezone.utc)


def at(seconds: int = 0) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def league() -> LeagueSnapshot:
    return LeagueSnapshot(
        league_id=123456,
        year=2026,
        name="Test League",
        draft_type="AUCTION",
        budget=200,
        team_count=12,
        my_team_id=4,
        roster={
            RosterSlot.QB: 1,
            RosterSlot.RB: 2,
            RosterSlot.WR: 2,
            RosterSlot.TE: 1,
            RosterSlot.FLEX: 1,
            RosterSlot.K: 1,
            RosterSlot.DST: 1,
            RosterSlot.BE: 7,
            RosterSlot.IR: 1,
        },
        flex_positions=(Position.RB, Position.WR, Position.TE),
    )


@pytest.fixture
def init_event(league) -> DraftInitialized:
    return DraftInitialized(
        id=1,
        at=at(),
        draft_id="2026-123456-20260816T190000",
        league=league,
        teams=tuple(TeamSeed(i, f"Team {i}") for i in range(1, 13)),
    )


def sold(
    event_id: int,
    name: str,
    team_id: int,
    price: int | None,
    *,
    pos: Position | None = None,
    seconds: int = 0,
    provenance: Provenance = Provenance.MANUAL,
) -> PlayerSold:
    moment = at(seconds)
    return PlayerSold(
        id=event_id,
        at=moment,
        player=PlayerRef.from_raw(name),
        team=known(team_id, provenance, moment),
        price=known(price, provenance, moment) if price is not None else unknown(moment),
        position=known(pos, provenance, moment) if pos else None,
    )
