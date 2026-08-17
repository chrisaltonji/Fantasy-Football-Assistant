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


def build_view(state: DraftState, book: Any = None, *, recent: int = 10) -> dict[str, Any]:
    if not state.is_initialized:
        return {
            "schema_version": VIEW_SCHEMA_VERSION,
            "draft_id": state.draft_id,
            "initialized": False,
        }

    league = state.league
    advisory = _advisory(state, book)
    return {
        "schema_version": VIEW_SCHEMA_VERSION,
        "draft_id": state.draft_id,
        "generated_at": to_iso(now_utc()),
        "initialized": True,
        "reference": _reference_meta(book),
        "league": {
            "name": league.name,
            "budget": league.budget,
            "team_count": league.team_count,
            "draftable_slots": league.draftable_slots,
            "roster_slots": {slot.value: n for slot, n in league.roster.items()},
            "flex_positions": [p.value for p in league.flex_positions],
        },
        "me": _team_view(state, league.my_team_id, book) if league.my_team_id in state.teams else None,
        "teams": [_team_view(state, team_id, book) for team_id in sorted(state.teams)],
        "nomination": _nomination_view(state, book, advisory),
        "market": _market_view(state, advisory),
        "recent_sales": _recent_sales(state, book, recent),
        "scarcity": _scarcity_view(advisory),
        "warnings": list(state.warnings) + list(getattr(book, "warnings", ())),
    }


def _advisory(state: DraftState, book: Any):
    """Run the deterministic advisory layer, if reference data is loaded."""
    if book is None or not len(book):
        return None
    from ffa.advice.engine import advise

    return advise(state, book)


def _reference_meta(book: Any) -> dict[str, Any] | None:
    """So a surface can say *whose* numbers these are — or warn they're fake."""
    if book is None or not len(book):
        return None
    return {
        "source": str(book.source) if book.source else None,
        "players": len(book),
        "scale": round(book.scale, 4),
        "is_sample": book.is_sample,
    }


def _scarcity_view(advisory) -> dict[str, Any]:
    if advisory is None:
        return {}
    return {
        position.value: {
            "elite": s.elite,
            "startable": s.startable,
            "bench": s.bench,
            "total_remaining": s.total_remaining,
            "starting_demand": s.starting_demand,
            "top_value_remaining": s.top_value_remaining,
            "is_drying_up": s.is_drying_up,
        }
        for position, s in advisory.scarcity.items()
    }


def _team_view(state: DraftState, team_id: int, book: Any = None) -> dict[str, Any]:
    team = state.teams.get(team_id)
    unknowns = proj.unknown_price_count(state, team_id)
    return {
        "team_id": team_id,
        # owner_id is the stable identity; name is volatile display text and
        # must not be used as a key by any surface.
        "owner_id": _sourced_value(team.owner_id) if team else None,
        "manager": _sourced_value(team.manager) if team else None,
        "name": _sourced_value(team.name) if team else None,
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
        "roster": [_player_view(p, book) for p in state.players_for_team(team_id)],
    }


def _player_view(player: PlayerEntity, book: Any = None) -> dict[str, Any]:
    position = _sourced_value(player.position)
    price = _sourced_value(player.price)
    value = book.value(player.ref.key) if (book is not None and len(book)) else None
    return {
        "key": player.ref.key,
        "name": player.ref.raw,
        "player_id": player.ref.player_id,
        "position": position.value if isinstance(position, Position) else position,
        "price": price,
        "price_known": player.price is not None and player.price.is_known,
        # Provenance travels with the value so a surface can show *why* it
        # believes a number — the audit trail the capability spec's
        # ground-truth-vs-inference rule depends on.
        "price_provenance": player.price.provenance.value if player.price else None,
        "reference_value": value,
        # Positive means they paid over the sheet. Feeds the "is the room hot"
        # read without the surface having to do arithmetic.
        "delta": (price - value) if (price is not None and value is not None) else None,
    }


def _nomination_view(state: DraftState, book: Any, advisory) -> dict[str, Any] | None:
    nomination = state.current_nomination
    if nomination is None:
        return None

    out: dict[str, Any] = {
        "key": nomination.ref.key,
        "name": nomination.ref.raw,
        "nominated_by": _sourced_value(nomination.nominated_by),
        "opening_bid": _sourced_value(nomination.opening_bid),
        "position": None,
        "reference_value": None,
        "guidance": None,
    }

    row = book.get(nomination.ref.key) if (book is not None and len(book)) else None
    if row is not None:
        out["position"] = row.position.value
        out["reference_value"] = book.value(row.key)

    guidance = advisory.guidance if advisory is not None else None
    if guidance is not None:
        out["guidance"] = {
            "max_legal_bid": guidance.max_legal_bid,
            "max_advisable_bid": guidance.max_advisable_bid,
            "suggested_low": guidance.suggested_low,
            "suggested_high": guidance.suggested_high,
            "inflated_value": guidance.inflated_value,
            "fills_starter_gap": guidance.fills_starter_gap,
            "contested_ceiling": guidance.contested_ceiling,
            "reasons": list(guidance.reasons),
            # Capacity only. Whether they *will* bid is the advisory layer's
            # call, made from the dossiers.
            "threats": [
                {
                    "team_id": t.team_id,
                    "label": t.label,
                    "max_legal_bid": t.max_legal_bid,
                    "remaining": t.remaining,
                    "remaining_is_floor": t.remaining_is_floor,
                    "has_starter_gap": t.has_starter_gap,
                    "open_at_position": t.open_at_position,
                    "is_live": t.is_live,
                }
                for t in guidance.threats
            ],
        }
    return out


def _market_view(state: DraftState, advisory) -> dict[str, Any]:
    totals = proj.league_totals(state)
    league = state.league
    out = {
        "sales": totals["sales"],
        "dollars_spent": totals["dollars_spent"],
        "unknown_prices": totals["unknown_prices"],
        "dollars_remaining": (league.budget * league.team_count) - totals["dollars_spent"],
        "inflation_ratio": None,
        "reference_spent": None,
        "read": "unknown",
    }
    if advisory is not None:
        out["inflation_ratio"] = advisory.market.inflation_ratio
        out["reference_spent"] = advisory.market.reference_spent
        out["read"] = advisory.market.read
    return out


def _recent_sales(state: DraftState, book: Any, limit: int) -> list[dict[str, Any]]:
    sold = [p for p in state.sold_players()]
    return [
        {**_player_view(p, book), "team_id": _sourced_value(p.team)}
        for p in sold[-limit:]
    ]


def _sourced_value(sourced):
    return sourced.value if sourced is not None and sourced.is_known else None
