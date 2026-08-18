"""The JSON view model — the contract between the engine and every surface.

This is the single shared draft-state object the advisory capability spec asks
for. The dashboard artifact, the ambient feed, and the chat thread all read
*this*, rather than each inventing its own store.

The dividing line it enforces is **provenance, not certainty**, and there are
three kinds of thing in here. Each lives in its own object rather than blended
into a team, so a surface can never mistake one for another:

- **Tonight's ground truth.** Budgets, slots, roster composition, counts,
  scarcity, bid ceilings — arithmetic over what has actually happened in this
  draft. If a number appears here, nothing downstream should recompute it.
- **A different draft's ground truth**, under the `history` key. Also
  deterministic, also arithmetic, but computed from *past* auctions. It is a
  fact about what somebody did before and a bet about what they do tonight, so
  the number of seasons behind it travels with every value.
- **Nothing computed at all**, under the `dossier` key. Recorded testimony about
  a person, in the words somebody chose.

Soft signals — who is *likely* to bid, notability, grades — are none of the
three. They belong to the advisory layer and are deliberately absent.

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


def build_view(
    state: DraftState,
    book: Any = None,
    *,
    recent: int = 10,
    dossiers: Any = None,
    precedent: Any = None,
    seats: Any = None,
) -> dict[str, Any]:
    if not state.is_initialized:
        return {
            "schema_version": VIEW_SCHEMA_VERSION,
            "draft_id": state.draft_id,
            "initialized": False,
        }

    league = state.league
    advisory = _advisory(state, book, precedent=precedent, seats=seats)
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
        "me": (
            _team_view(state, league.my_team_id, book, dossiers, precedent, seats)
            if league.my_team_id in state.teams
            else None
        ),
        "teams": [
            _team_view(state, team_id, book, dossiers, precedent, seats)
            for team_id in sorted(state.teams)
        ],
        "nomination": _nomination_view(state, book, advisory, dossiers),
        # Top level, and deliberately not nested under `nomination`: that key is
        # `None` whenever nothing is on the block, which is exactly the moment
        # whose-turn-is-it and what-to-put-up are worth reading.
        "nomination_plan": _nomination_plan_view(advisory),
        "market": _market_view(state, advisory),
        "recent_sales": _recent_sales(state, book, recent),
        "scarcity": _scarcity_view(advisory),
        "warnings": list(state.warnings) + list(getattr(book, "warnings", ())),
    }


def _advisory(state: DraftState, book: Any, *, precedent=None, seats=None):
    """Run the deterministic advisory layer, if reference data is loaded."""
    if book is None or not len(book):
        return None
    from ffa.advice.engine import advise

    return advise(state, book, precedent=precedent, seats=seats)


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


def _team_view(
    state: DraftState, team_id: int, book: Any = None, dossiers: Any = None,
    precedent: Any = None, seats: Any = None,
) -> dict[str, Any]:
    team = state.teams.get(team_id)
    unknowns = proj.unknown_price_count(state, team_id)
    return {
        # Recorded observations about the *person*, never a computed likelihood.
        # See `_dossier_view` for why that distinction is load-bearing.
        "dossier": _dossier_view(dossiers, team_id),
        # Measured from this manager's *past* drafts. Computed, unlike the
        # dossier, but from a different draft's ground truth than everything
        # below it. See `_history_view`.
        "history": _history_view(precedent, team_id, seats),
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


def _turn_view(turn) -> dict[str, Any] | None:
    if turn is None:
        return None
    return {
        "index": turn.index,
        "pick_number": turn.index + 1,
        "round": turn.round_number,
        "team_id": turn.team_id,
        "label": turn.label,
        "is_me": turn.is_me,
    }


def _candidate_view(candidate) -> dict[str, Any]:
    return {
        "key": candidate.key,
        "name": candidate.name,
        "position": candidate.position.value if candidate.position else None,
        "value": candidate.value,
        "live_rivals": candidate.live_rivals,
        "contested_ceiling": candidate.contested_ceiling,
        "fills_my_starter_gap": candidate.fills_my_starter_gap,
        "my_ceiling": candidate.my_ceiling,
    }


def _nomination_plan_view(advisory) -> dict[str, Any] | None:
    """Capability 4. Arithmetic throughout, and honest about what it omits.

    `order_known: false` is a real state, not an error — ESPN has no pickOrder
    until the commissioner sets the draft order, and every seat named here would
    be a guess until then. A surface must check it before rendering a turn.

    The truncated lists ship their own totals. A dashboard that renders five
    bargains and says "5" when there are nineteen is worse than one that renders
    none, because it reads as a complete answer.
    """
    plan = advisory.nomination if advisory is not None else None
    if plan is None:
        return None

    return {
        "order_known": plan.order_known,
        "turns_taken": plan.turns_taken,
        "total_nominations": plan.total_nominations,
        "on_the_clock": _turn_view(plan.on_the_clock),
        "current_nominator": _turn_view(plan.current_nominator),
        "upcoming": [_turn_view(t) for t in plan.upcoming],
        "my_next": _turn_view(plan.my_next),
        "my_turns_left": plan.my_turns_left,
        "nominations_until_mine": plan.nominations_until_mine,
        "is_my_turn": plan.is_my_turn,
        # Players nobody who needs them can afford — free money on our turn.
        "bargains": [_candidate_view(c) for c in plan.bargains],
        "bargains_total": plan.bargains_total,
        # Players priced past our own ceiling that a live rival can actually pay
        # for. Putting one up cannot cost us somebody we could have won; whether
        # it is *wise* is inference, and this does not claim it is.
        "out_of_reach": [_candidate_view(c) for c in plan.out_of_reach],
        "out_of_reach_total": plan.out_of_reach_total,
        # False means the board was never priced, so two empty lists above mean
        # "not looked at" rather than "nothing there".
        "board_read": plan.board_read,
        "disagreement": plan.disagreement,
    }


def _nomination_view(
    state: DraftState, book: Any, advisory, dossiers: Any = None
) -> dict[str, Any] | None:
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
            # The same ceiling with our own unpriced picks charged at what they
            # probably cost. `max_legal_bid` charges them $1, which over-states
            # what we have; every surface should act on this one.
            "safe_legal_bid": guidance.safe_legal_bid,
            "unknown_prices": guidance.unknown_prices,
            "ceiling_is_optimistic": guidance.ceiling_is_optimistic,
            "max_advisable_bid": guidance.max_advisable_bid,
            "suggested_low": guidance.suggested_low,
            "suggested_high": guidance.suggested_high,
            "inflated_value": guidance.inflated_value,
            "fills_starter_gap": guidance.fills_starter_gap,
            "contested_ceiling": guidance.contested_ceiling,
            "reasons": list(guidance.reasons),
            # Rivals against their own precedent. Arithmetic, but over previous
            # drafts, and empty once the record stops discriminating.
            "pace_reads": [
                {
                    "team_id": r.team_id,
                    "label": r.label,
                    "spent": r.spent,
                    "actual_share": round(r.actual_share, 4),
                    "expected_share": round(r.expected_share, 4),
                    "dollars_vs_script": r.dollars_vs_script,
                    "seasons": r.seasons,
                    "is_thin": r.is_thin,
                }
                for r in guidance.pace_reads
                if r.is_notable
            ],
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
                    # Everything above this line is arithmetic. This is not: it
                    # is what somebody told us about a person, verbatim, so the
                    # layer that reasons about intent has something to reason
                    # from. No likelihood is computed here, and none should be
                    # inferred from its presence.
                    "dossier": _dossier_view(dossiers, t.team_id),
                }
                for t in guidance.threats
            ],
        }
    return out


def _dossier_view(dossiers: Any, team_id: int) -> dict[str, Any] | None:
    """What we were told about the person in this seat. Observations, not scores.

    This is the one part of the payload that is not *computed* at all, and
    keeping that legible is the point of shipping it as a separate object rather
    than folding fields into the team. Two other things here are computed: most
    of the payload, from tonight's board, and the sibling `history` block, from
    past drafts. This is neither. Nothing here was measured — it is what somebody
    told us about a person, and the layer that reads it is expected to add
    judgment on top.

    Concretely: there is no `will_bid`, no `likelihood`, no score. The dashboard
    spec asks for "room for a likelihood annotation per bidder without implying
    the engine supplied it", and an engine that shipped one would be claiming to
    know something it cannot.

    `None` when there is no dossier at all, so a surface can distinguish "we
    have not asked about this manager" from "we asked and they are unremarkable".
    """
    if dossiers is None:
        return None
    dossier = dossiers.for_team(team_id)
    if dossier is None or dossier.is_empty:
        return None

    from ffa.dossier.schema import QUESTIONS, render_answer

    out: dict[str, Any] = {
        "owner_id": dossier.owner_id,
        "coverage": round(dossier.coverage, 3),
        "updated_at": dossier.updated_at or None,
    }
    for question in QUESTIONS:
        value = getattr(dossier, question.field, None)
        rendered = render_answer(question, value)
        out[question.field] = None if rendered == "-" else rendered
    return out


def _history_view(precedent: Any, team_id: int, seats: Any) -> dict[str, Any] | None:
    """What this manager's own past auctions say. Measured, not asserted.

    Computed, like almost everything else in this payload — but from a
    *different* draft's ground truth, which is the distinction the sibling
    `dossier` key makes from the other side. A number here is a fact about 2023;
    whether it predicts tonight is the reading layer's call, so `seasons`
    travels with every value and a manager with no record gets `None` rather
    than a zero.

    Nothing here is a likelihood and nothing here moves a bid ceiling.
    `max_advisable_bid` is arithmetic over tonight's board and stays that way;
    this sits beside it. Only signals that cleared a permutation test in
    `ffa.history.evidence` are present at all.
    """
    if precedent is None or not precedent:
        return None
    script = precedent.for_team(team_id, seats or {})
    if script is None:
        return None
    return {
        "seasons": list(script.seasons),
        "accounts": len(script.accounts) or 1,
        "spend_curve": list(script.curve),
        "curve_spread": list(script.spread),
        "useful_through": precedent.window,
        "spend_shape": script.spend_shape or None,
        "pace": script.pace or None,
        "nomination_premium": script.nomination_premium,
        "te_share": script.te_share,
        "is_thin": script.is_thin,
    }


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
