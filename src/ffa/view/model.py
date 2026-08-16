"""The JSON view model — the contract between the engine and every surface.

This is the single shared draft-state object the advisory capability spec asks
for. The dashboard artifact, the ambient feed, and the chat thread all read
*this*, rather than each inventing its own store.

The dividing line it enforces: **everything in here is deterministic.** Budgets,
slots, roster composition, counts — computed by the engine from ground truth,
never re-derived downstream. Soft signals (who's likely to bid, tendency reads,
notability, grades) belong to the advisory layer and are deliberately absent.
If a number appears here, nothing downstream should be recomputing it.

Keeping draft state in a file rather than only in a conversation is also what
lets a 3+ hour draft stay workable: the thread can be summarized or restarted
without losing the board.

CP3 adds `scarcity`, per-player `value`, and the inflation ratio once reference
data exists. The keys are reserved here so the shape doesn't churn under the
dashboard design.
"""

from __future__ import annotations

from typing import Any

from ffa.domain import projections as proj
from ffa.domain.enums import Position
from ffa.domain.models import DraftState, PlayerEntity
from ffa.util.clock import now_utc, to_iso

VIEW_SCHEMA_VERSION = 1


def build_view(state: DraftState, *, recent: int = 10) -> dict[str, Any]:
    if not state.is_initialized:
        return {
            "schema_version": VIEW_SCHEMA_VERSION,
            "draft_id": state.draft_id,
            "initialized": False,
        }

    league = state.league
    return {
        "schema_version": VIEW_SCHEMA_VERSION,
        "draft_id": state.draft_id,
        "generated_at": to_iso(now_utc()),
        "initialized": True,
        "league": {
            "name": league.name,
            "budget": league.budget,
            "team_count": league.team_count,
            "draftable_slots": league.draftable_slots,
            "roster_slots": {slot.value: n for slot, n in league.roster.items()},
            "flex_positions": [p.value for p in league.flex_positions],
        },
        "me": _team_view(state, league.my_team_id) if league.my_team_id in state.teams else None,
        "teams": [_team_view(state, team_id) for team_id in sorted(state.teams)],
        "nomination": _nomination_view(state),
        "market": _market_view(state),
        "recent_sales": _recent_sales(state, recent),
        # Reserved for CP3, when rankings and tiers exist. Present-but-empty
        # so the dashboard can bind to the key now.
        "scarcity": {},
        "warnings": list(state.warnings),
    }


def _team_view(state: DraftState, team_id: int) -> dict[str, Any]:
    team = state.teams.get(team_id)
    unknowns = proj.unknown_price_count(state, team_id)
    return {
        "team_id": team_id,
        "name": _sourced_value(team.name) if team else None,
        "manager": _sourced_value(team.manager) if team else None,
        "label": team.label if team else f"team{team_id}",
        "is_me": state.league is not None and team_id == state.league.my_team_id,
        "spent": proj.spent(state, team_id),
        "remaining": proj.remaining_budget(state, team_id),
        # True when `remaining` is a floor rather than a fact, because some
        # prices are still unknown and charged at the $1 minimum.
        "remaining_is_floor": unknowns > 0,
        "unknown_price_count": unknowns,
        "max_legal_bid": proj.max_legal_bid(state, team_id),
        "is_overspent": proj.is_overspent(state, team_id),
        "roster_count": proj.roster_count(state, team_id),
        "open_slots": proj.open_draftable_slots(state, team_id),
        "open_slots_by_pos": {
            pos.value: n for pos, n in proj.open_slots_by_pos(state, team_id).items()
        },
        "starter_gaps": {
            slot.value: n for slot, n in proj.starter_gaps(state, team_id).items()
        },
        "roster": [_player_view(p) for p in state.players_for_team(team_id)],
    }


def _player_view(player: PlayerEntity) -> dict[str, Any]:
    position = _sourced_value(player.position)
    return {
        "key": player.ref.key,
        "name": player.ref.raw,
        "player_id": player.ref.player_id,
        "position": position.value if isinstance(position, Position) else position,
        "price": _sourced_value(player.price),
        "price_known": player.price is not None and player.price.is_known,
        # Provenance travels with the value so a surface can show *why* it
        # believes a number — the audit trail the capability spec's
        # ground-truth-vs-inference rule depends on.
        "price_provenance": player.price.provenance.value if player.price else None,
    }


def _nomination_view(state: DraftState) -> dict[str, Any] | None:
    nomination = state.current_nomination
    if nomination is None:
        return None
    return {
        "key": nomination.ref.key,
        "name": nomination.ref.raw,
        "nominated_by": _sourced_value(nomination.nominated_by),
        "opening_bid": _sourced_value(nomination.opening_bid),
    }


def _market_view(state: DraftState) -> dict[str, Any]:
    totals = proj.league_totals(state)
    league = state.league
    return {
        "sales": totals["sales"],
        "dollars_spent": totals["dollars_spent"],
        "unknown_prices": totals["unknown_prices"],
        "dollars_remaining": (league.budget * league.team_count) - totals["dollars_spent"],
        # CP3 fills this in once reference auction values exist.
        "inflation_ratio": None,
    }


def _recent_sales(state: DraftState, limit: int) -> list[dict[str, Any]]:
    sold = [p for p in state.sold_players()]
    return [
        {**_player_view(p), "team_id": _sourced_value(p.team)}
        for p in sold[-limit:]
    ]


def _sourced_value(sourced):
    return sourced.value if sourced is not None and sourced.is_known else None
