"""What each agent actually gets to see.

`build_view()` is the contract every surface reads, and it is ~6,900 tokens with
the `teams` block alone accounting for 4,581 that churn on every pick. Handing all
of it over per nomination is wasteful, but the worse problem is attention: the
nominated player ends up buried under eleven complete rosters, and the model spends
its reasoning re-deriving what the board already computed.

So each agent gets a projection. These are **derived from the contract, not a
second source of truth** — pure `dict -> dict`, no I/O, no SDK, testable against
`docs/sample_state.json` in microseconds. Nothing here recomputes a number; it
selects.

**The one non-obvious cut is `guidance.threats[].dossier`.** `view/model.py` puts a
full dossier inside every threat, which is right for a surface that renders one
player at a time. But the same testimony is already in the cached prefix, word for
word, for every manager in the league. Shipping both pays for it twice on every
nomination *and* shows the model the same evidence in two different shapes, which
is how a reply starts citing one and contradicting the other. Strip it; join on
`team_id`.
"""

from __future__ import annotations

from typing import Any

# How many recent sales carry useful signal about the room's temperature. Beyond
# this it is history, and `market.inflation_ratio` already summarises it.
RECENT_SALES = 5


def _rival_line(team: dict[str, Any]) -> dict[str, Any]:
    """One rival, compressed to what bears on a bid.

    No roster, no dossier, no history — the first is noise at this scale and the
    other two are in the cached prefix.
    """
    return {
        "team_id": team.get("team_id"),
        "label": team.get("label"),
        "remaining": team.get("remaining"),
        "remaining_is_floor": team.get("remaining_is_floor"),
        "max_legal_bid": team.get("max_legal_bid"),
        "roster_count": team.get("roster_count"),
        "starter_gaps": team.get("starter_gaps"),
        "open_slots_by_pos": team.get("open_slots_by_pos"),
    }


def _my_line(me: dict[str, Any]) -> dict[str, Any]:
    """Our own team. Roster kept, but as names and prices only."""
    return {
        "label": me.get("label"),
        "spent": me.get("spent"),
        "remaining": me.get("remaining"),
        "remaining_is_floor": me.get("remaining_is_floor"),
        "max_legal_bid": me.get("max_legal_bid"),
        "roster_count": me.get("roster_count"),
        "open_slots": me.get("open_slots"),
        "starter_gaps": me.get("starter_gaps"),
        "open_slots_by_pos": me.get("open_slots_by_pos"),
        "roster": [
            {"name": p.get("name"), "position": p.get("position"), "price": p.get("price")}
            for p in (me.get("roster") or [])
        ],
    }


def _schedule_only(plan: dict[str, Any] | None) -> dict[str, Any]:
    """The nomination schedule, without capability 4's candidate lists.

    `bargains` and `out_of_reach` are a different question — what to put up on
    your own turn — and including them here invites the Room to answer that
    instead of the one it was asked.
    """
    if not plan:
        return {}
    return {
        key: plan.get(key) for key in (
            "order_known", "turns_taken", "total_nominations", "on_the_clock",
            "current_nominator", "my_next", "nominations_until_mine",
            "my_turns_left", "is_my_turn",
        )
    }


def _scarcity_focus(scarcity: dict[str, Any], position: str | None) -> dict[str, Any]:
    """The nominated position in full; every other one as a single line.

    Depth at running back matters when a running back is up. When a tight end is
    up it is one number.
    """
    out: dict[str, Any] = {}
    for pos, block in (scarcity or {}).items():
        if pos == position:
            out[pos] = block
        else:
            out[pos] = {
                "startable": block.get("startable"),
                "starting_demand": block.get("starting_demand"),
                "is_drying_up": block.get("is_drying_up"),
            }
    return out


def _strategy_line(strategy: dict[str, Any] | None) -> dict[str, Any]:
    if not strategy:
        return {}
    return {
        key: strategy.get(key) for key in (
            "archetype", "remaining", "still_calls_for", "shortfall", "slack",
            "max_on_one_player", "bench_reserve",
        )
    }


def _sales_tail(sales: list[dict[str, Any]] | None, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "name": s.get("name"), "position": s.get("position"),
            "price": s.get("price"), "reference_value": s.get("reference_value"),
            "delta": s.get("delta"), "team_id": s.get("team_id"),
        }
        for s in (sales or [])[-limit:]
    ]


def room_payload(view: dict[str, Any], *, prior_reads: list | None = None
                 ) -> dict[str, Any]:
    """What the Room sees when a player goes on the block. ~1,800 tokens."""
    # Nothing on the block is the board's normal state, and the Room should not
    # be called then. If it is, say so plainly rather than handing over an empty
    # guidance block that reads like a player with no threats.
    if not view.get("nomination"):
        return {
            "nomination": {},
            "me": _my_line(view.get("me") or {}),
            "rivals": [_rival_line(t) for t in (view.get("teams") or [])
                       if not t.get("is_me")],
            "market": view.get("market") or {},
            "scarcity": _scarcity_focus(view.get("scarcity") or {}, None),
            "nomination_plan": _schedule_only(view.get("nomination_plan")),
            "watchlist": view.get("watchlist") or [],
            "strategy": _strategy_line(view.get("strategy")),
            "recent_sales": _sales_tail(view.get("recent_sales"), RECENT_SALES),
            "prior_reads": prior_reads or [],
        }

    nomination = dict(view.get("nomination") or {})
    guidance = dict(nomination.get("guidance") or {})

    # The cut that matters. See the module docstring.
    threats = []
    for threat in guidance.get("threats") or []:
        stripped = {k: v for k, v in threat.items() if k != "dossier"}
        threats.append(stripped)
    guidance["threats"] = threats
    nomination["guidance"] = guidance

    position = nomination.get("position")
    return {
        "nomination": nomination,
        "me": _my_line(view.get("me") or {}),
        "rivals": [_rival_line(t) for t in (view.get("teams") or []) if not t.get("is_me")],
        "market": view.get("market") or {},
        "scarcity": _scarcity_focus(view.get("scarcity") or {}, position),
        "nomination_plan": _schedule_only(view.get("nomination_plan")),
        "watchlist": view.get("watchlist") or [],
        "strategy": _strategy_line(view.get("strategy")),
        "recent_sales": _sales_tail(view.get("recent_sales"), RECENT_SALES),
        "prior_reads": prior_reads or [],
    }


def strategist_payload(view: dict[str, Any], *, sale: dict[str, Any] | None = None,
                       reconciled: dict[str, Any] | None = None,
                       plan_state: str | None = None) -> dict[str, Any]:
    """What the Strategist sees after a pick lands.

    Our own team in full and the plan in full — it is the only agent whose whole
    subject is us. `plan_state` is passed in rather than left to be derived: it is
    computed by `advice/strategy.py`, and the schema asks for it to be echoed so
    the guard can check the reply against it.
    """
    return {
        "plan_state": plan_state,
        "me": view.get("me") or {},
        "strategy": view.get("strategy") or {},
        "market": view.get("market") or {},
        "scarcity": view.get("scarcity") or {},
        "watchlist": view.get("watchlist") or [],
        "nomination_plan": _schedule_only(view.get("nomination_plan")),
        "rivals": [_rival_line(t) for t in (view.get("teams") or []) if not t.get("is_me")],
        "sale": sale or {},
        "reconciled": reconciled or {},
        "recent_sales": _sales_tail(view.get("recent_sales"), RECENT_SALES),
    }


def narrator_payload(view: dict[str, Any], *, entry: dict[str, Any],
                     reconciled: dict[str, Any] | None = None,
                     prior_read: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the Narrator sees. Deliberately tiny — ~400 tokens.

    It writes one sentence about one event. Handing it the board would dilute
    that into a summary of the draft, which is the failure mode the deterministic
    feed was already built to avoid.
    """
    position = entry.get("position")
    scarcity = (view.get("scarcity") or {}).get(position) if position else None
    return {
        "entry": entry,
        "market": view.get("market") or {},
        "position_scarcity": {position: scarcity} if scarcity else {},
        "reconciled": reconciled or {},
        "what_we_said": prior_read or {},
    }


def analyst_payload(view: dict[str, Any], *, question: str,
                    prior_reads: list | None = None) -> dict[str, Any]:
    """What the Analyst sees: everything.

    On demand, not latency-bound, and the question could be about any corner of
    the board — so this is the one place the full payload is the right trade.
    """
    return {
        "question": question,
        "board": view,
        "prior_reads": prior_reads or [],
    }


def grader_payload(view: dict[str, Any], *, reads: list, feed: list | None = None
                   ) -> dict[str, Any]:
    """What the Grader sees once the draft is over.

    Rosters matter here and nowhere else — grading a draft means looking at what
    was actually assembled. `reads` is the whole log, because scoring the
    assistant against outcomes is half the job.
    """
    return {
        "me": view.get("me") or {},
        "teams": view.get("teams") or [],
        "strategy": view.get("strategy") or {},
        "market": view.get("market") or {},
        "scarcity": view.get("scarcity") or {},
        "feed": feed or (view.get("feed") or []),
        "reads": reads,
    }
