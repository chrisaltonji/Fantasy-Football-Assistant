"""Events in, state out. Pure, total, and never raises.

Two properties everything downstream depends on:

**Reducers may not reject an event.** By the time `apply` sees an event it is
already durably on disk. If a reducer could refuse it, the journal and the
state would disagree and a resume would produce a different draft than the
live session did. Anomalies become warnings; validation that can actually say
no lives at parse and command time, before anything is appended.

**Reducers are order-independent over the surviving event set.** Applying
`[sold, amend]` and `[amend, sold]` with the same observation timestamps must
converge, because `merge` — not arrival order — decides who wins. That is the
operational definition of replay determinism here, and it is why amendments
auto-vivify entities instead of being dropped when they arrive early.

Reducers never read the clock. All timestamps come from the events themselves.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable

from ffa.domain.events import (
    BaseEvent,
    DraftInitialized,
    EventUndone,
    FieldAmended,
    PlayerNominated,
    PlayerSold,
    UnknownEvent,
)
from ffa.domain.ids import PLAYER, TEAM, EntityAddressError, PlayerRef, parse_entity_address
from ffa.domain.models import DraftState, NominationEntity, PlayerEntity, TeamEntity
from ffa.domain.projections import is_overspent, remaining_budget
from ffa.domain.sourced import merge


def undone_ids(events: Iterable[BaseEvent]) -> frozenset[int]:
    """Which event ids are currently suppressed.

    Pass 1 of the two-pass replay. Un-applying a merge is *impossible* — merge
    is lossy, and the pre-merge value cannot be recovered from the post-merge
    one — so there is no inverse-apply anywhere in this codebase. Replaying
    ~300 events from zero is free; reconstructing history is not.

    Undoing an `EventUndone` is redo, and falls out of the same loop rather
    than needing a sixth event type.
    """
    events = list(events)
    undos = {e.id: e for e in events if isinstance(e, EventUndone)}
    suppressed: set[int] = set()

    for event in events:
        if not isinstance(event, EventUndone):
            continue
        target = event.target_id
        if target in suppressed:
            # Already undone. The command layer rejects this; a hand-edited
            # journal can still contain it, so ignore rather than crash.
            continue
        if target in undos:
            # Redo: reverse what that undo suppressed, then retire the undo.
            suppressed.discard(undos[target].target_id)
            suppressed.add(target)
        else:
            suppressed.add(target)

    return frozenset(suppressed)


def replay(events: Iterable[BaseEvent], draft_id: str = "") -> DraftState:
    """Rebuild state from scratch. The only path that undo needs."""
    events = list(events)
    suppressed = undone_ids(events)
    state = DraftState.empty(draft_id)
    for event in events:
        if event.id in suppressed or isinstance(event, EventUndone):
            continue
        state = apply(state, event)
    return state


def apply(state: DraftState, event: BaseEvent) -> DraftState:
    """Fold one event into state. Dispatch is an explicit table, not isinstance
    chains or `singledispatch`, so every branch is greppable and testable alone.
    """
    reducer = _REDUCERS.get(type(event))
    if reducer is None:
        name = getattr(event, "type_name", type(event).__name__)
        return state.with_warning(f"ignored unrecognized event #{event.id} ({name})")
    return reducer(state, event)


# --- individual reducers -----------------------------------------------------


def _apply_initialized(state: DraftState, event: DraftInitialized) -> DraftState:
    teams = {
        seed.team_id: TeamEntity(
            team_id=seed.team_id,
            owner_id=_seed_value(seed.owner_id, event),
            manager=_seed_value(seed.manager, event),
            name=_seed_value(seed.name, event),
        )
        for seed in event.teams
    }
    return replace(state, draft_id=event.draft_id, league=event.league, teams=teams,
                   applied_ids=state.applied_ids + (event.id,))


def _apply_nominated(state: DraftState, event: PlayerNominated) -> DraftState:
    player = _player(state, event.player)
    players = dict(state.players)
    players[player.ref.key] = replace(player, nominated=True)
    nomination = NominationEntity(
        event_id=event.id,
        ref=event.player,
        nominated_by=event.nominated_by,
        opening_bid=event.opening_bid,
    )
    return replace(state, players=players, current_nomination=nomination,
                   applied_ids=state.applied_ids + (event.id,))


def _apply_sold(state: DraftState, event: PlayerSold) -> DraftState:
    player = _player(state, event.player)
    updated = replace(
        player,
        team=merge(player.team, event.team),
        price=merge(player.price, event.price),
        position=merge(player.position, event.position) if event.position else player.position,
        sold=True,
    )
    players = dict(state.players)
    players[player.ref.key] = updated

    nomination = state.current_nomination
    if nomination is not None and nomination.ref.key == player.ref.key:
        nomination = None

    state = replace(state, players=players, current_nomination=nomination,
                    applied_ids=state.applied_ids + (event.id,))
    return _flag_overspend(state, updated)


def _apply_amended(state: DraftState, event: FieldAmended) -> DraftState:
    try:
        kind, ident = parse_entity_address(event.entity)
    except EntityAddressError as exc:
        return state.with_warning(f"event #{event.id}: {exc}")

    if kind == PLAYER:
        return _amend_player(state, event, str(ident))
    if kind == TEAM:
        return _amend_team(state, event, int(ident))
    return state.with_warning(
        f"event #{event.id}: nothing amendable at {event.entity!r}"
    )


def _amend_player(state: DraftState, event: FieldAmended, key: str) -> DraftState:
    # Auto-vivify. An amendment can legitimately arrive before its sale (ESPN
    # lagging) or outlive it (the sale was undone), and in both cases the
    # result must be the same as if it had arrived in the other order.
    player = state.players.get(key) or PlayerEntity(ref=PlayerRef(key=key, raw=key))

    field = event.field_name
    if field == "price":
        player = replace(player, price=merge(player.price, event.value))
    elif field == "team":
        player = replace(player, team=merge(player.team, event.value))
    elif field == "position":
        player = replace(player, position=merge(player.position, event.value))
    elif field in ("name", "player_id"):
        ref = replace(
            player.ref,
            raw=event.value.value if field == "name" and event.value.is_known else player.ref.raw,
            player_id=event.value.value if field == "player_id" else player.ref.player_id,
        )
        player = replace(player, ref=ref)
    else:
        return state.with_warning(f"event #{event.id}: players have no field {field!r}")

    players = dict(state.players)
    players[key] = player
    state = replace(state, players=players, applied_ids=state.applied_ids + (event.id,))
    return _flag_overspend(state, player)


def _amend_team(state: DraftState, event: FieldAmended, team_id: int) -> DraftState:
    team = state.teams.get(team_id) or TeamEntity(team_id=team_id)
    field = event.field_name
    if field == "manager":
        team = replace(team, manager=merge(team.manager, event.value))
    elif field == "owner_id":
        team = replace(team, owner_id=merge(team.owner_id, event.value))
    elif field == "name":
        team = replace(team, name=merge(team.name, event.value))
    else:
        return state.with_warning(f"event #{event.id}: teams have no field {field!r}")

    teams = dict(state.teams)
    teams[team_id] = team
    return replace(state, teams=teams, applied_ids=state.applied_ids + (event.id,))


def _apply_undone(state: DraftState, event: EventUndone) -> DraftState:
    """No-op by design. Undo is a replay filter, never a state mutation."""
    return state


def _apply_unknown(state: DraftState, event: UnknownEvent) -> DraftState:
    return state.with_warning(
        f"skipped event #{event.id} of unknown type {event.type_name!r} — "
        "this journal was written by a newer version of ffa"
    )


# --- helpers -----------------------------------------------------------------


def _player(state: DraftState, ref: PlayerRef) -> PlayerEntity:
    existing = state.players.get(ref.key)
    if existing is None:
        return PlayerEntity(ref=ref)
    # Keep whatever richer ref we already had (a CP3-resolved player_id, say)
    # while letting a later raw spelling through.
    return replace(existing, ref=replace(existing.ref, raw=ref.raw or existing.ref.raw))


def _seed_value(raw: str, event: DraftInitialized):
    if not raw:
        return None
    from ffa.domain.enums import Provenance
    from ffa.domain.sourced import known

    return known(raw, Provenance.ESPN_API if event.source == "ESPN_API" else Provenance.MANUAL,
                 event.at) if event.at else None


def _flag_overspend(state: DraftState, player: PlayerEntity) -> DraftState:
    """Record, never reject.

    Our model of a team's money can be wrong — a keeper, a price we never
    learned, a pick entered against the wrong team. ESPN is the source of truth
    for what actually happened, and the tool is not entitled to refuse reality.
    """
    if player.team is None or not player.team.is_known:
        return state
    team_id = player.team.value
    if not is_overspent(state, team_id):
        return state
    label = state.teams[team_id].label if team_id in state.teams else f"team{team_id}"
    return state.with_warning(
        f"{label} is ${abs(remaining_budget(state, team_id))} over budget — "
        "check for a missed pick or a wrong price"
    )


_REDUCERS: dict[type, Callable[[DraftState, BaseEvent], DraftState]] = {
    DraftInitialized: _apply_initialized,
    PlayerNominated: _apply_nominated,
    PlayerSold: _apply_sold,
    FieldAmended: _apply_amended,
    EventUndone: _apply_undone,
    UnknownEvent: _apply_unknown,
}
