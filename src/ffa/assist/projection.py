"""What each agent actually gets to see.

`build_view()` is the contract every surface reads, and it is ~15,200 tokens with
the `teams` block alone accounting for 10,300 that churn on every pick. Handing
all of it over per nomination is wasteful, but the worse problem is attention:
the nominated player ends up buried under eleven complete rosters, and the model
spends its reasoning re-deriving what the board already computed.

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


def _rival_line(team: dict[str, Any],
                threat: dict[str, Any] | None = None) -> dict[str, Any]:
    """One rival, compressed to what bears on a bid.

    No roster, no dossier, no history — the first is noise at this scale and the
    other two are in the cached prefix.

    **`open_slots_by_pos` is deliberately absent.** The glossary tells the model
    in as many words that it includes the bench, is almost always non-zero, and
    is therefore not evidence of need. Sending a field while instructing the
    reader to disregard it is six keys per rival of pure cost.

    `threat` folds in this player-specific verdict — chiefly `is_live`, which the
    glossary calls the single most important flag on the board. It arrives here
    rather than in a parallel list; see `room_payload`.
    """
    threat = threat or {}
    line = {
        "team_id": team.get("team_id"),
        "label": team.get("label"),
        "remaining": team.get("remaining"),
        "remaining_is_floor": team.get("remaining_is_floor"),
        "max_legal_bid": team.get("max_legal_bid"),
        "roster_count": team.get("roster_count"),
        "starter_gaps": team.get("starter_gaps"),
    }
    if threat:
        line["is_live"] = threat.get("is_live")
        line["open_at_position"] = threat.get("open_at_position")
    return line


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
        # Dropped for the same reason as on the rivals: the glossary tells the
        # model it is not evidence of need, and `open_slots` already says how
        # many players we still have to buy.
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
    """What the Room sees when a player goes on the block.

    3,200 tokens at the first nomination and ~4,400 mid-draft, measured with
    `count_tokens` against the real league — a quarter of the full view.
    """
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

    # The cut that matters, and it is bigger than it looks. `guidance.threats`
    # and `teams` are the *same eleven managers*, described twice and sharing
    # five fields apiece — together 77% of this payload, measured. Worse, it is
    # the failure the dossier strip below already names: one thing in two
    # shapes, which is how a reply starts citing one and contradicting the
    # other.
    #
    # So the threats are folded into `rivals` and the array is dropped. The two
    # fields that are genuinely per-player rather than per-team — `is_live` and
    # `open_at_position` — travel on the rival line where they are read. The
    # dossier goes for the original reason: the same testimony is already in the
    # cached prefix, word for word.
    #
    # `guard.clamp_rivals` still checks estimates against the real threat list,
    # which it takes from the *view* on the main thread. What the model is shown
    # and what its answer is checked against stay separate on purpose.
    threats = {
        t.get("team_id"): {k: v for k, v in t.items() if k != "dossier"}
        for t in (guidance.get("threats") or [])
    }
    guidance.pop("threats", None)
    nomination["guidance"] = guidance

    position = nomination.get("position")
    return {
        "nomination": nomination,
        "me": _my_line(view.get("me") or {}),
        "rivals": [_rival_line(t, threats.get(t.get("team_id")))
                   for t in (view.get("teams") or []) if not t.get("is_me")],
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
