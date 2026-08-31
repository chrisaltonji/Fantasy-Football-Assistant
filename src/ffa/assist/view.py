"""The ledger, shaped for a surface to render.

`build_view(assist=...)` takes whatever this returns. It is deliberately a
separate function rather than a method on `ReadLog`, for the same reason
`projection.py` is separate from `view/model.py`: the store should not know what
a dashboard looks like, and a surface should not have to understand record
statuses.

**Everything here is about making provenance impossible to miss.** A read renders
next to `max_advisable_bid`, which is arithmetic, and anything printed beside a
computed number inherits its authority unless something actively prevents that.
So every entry carries `is_inference: True`, the agent that said it, and the
moment it was said about.

**Only `ok` records reach a surface.** `rejected` means the guard caught a verdict
or an impossible estimate; `late` means the player had already sold. Both stay in
the ledger — they are what the Grader scores the assistant on, and how often they
happen is the thing you would want to know before trusting any of it — but
neither is something to put on screen as though it stood.
"""

from __future__ import annotations

from typing import Any

# How many past reads a surface gets. The dashboard shows a running column; the
# whole draft would be 180 entries nobody scrolls.
RECENT_READS = 12


def _entry(record: Any) -> dict[str, Any]:
    """One read, flattened for rendering and labelled as inference."""
    return {
        "seq": record.seq,
        "at": record.at,
        "agent": record.agent,
        "player_key": record.player_key,
        "event_id": record.event_id,
        "payload": record.payload,
        # Not decoration. This is the flag a template keys its styling off, so a
        # read can never be rendered in the same voice as a ceiling.
        "is_inference": True,
    }


def assist_view(log: Any, *, runner: Any = None, player_key: str = "",
                recent: int = RECENT_READS) -> dict[str, Any]:
    """What the `assist` key holds, or `{}` when there is nothing to say.

    Returning `{}` rather than a populated-but-empty object matters: `build_view`
    omits the key entirely on a falsy value, which is what keeps the payload
    byte-identical for anyone who never turns the assistant on.
    """
    if log is None:
        return {}

    records = [r for r in log.all() if r.status == "ok"]
    counts = log.counts()

    # The current read is looked up by player rather than by recency: a read that
    # arrived after its player sold is recorded `late` and must never be shown
    # as though it were about whoever is on the block now.
    current = None
    revision = None
    if player_key:
        latest = log.latest("room", player_key)
        if latest is not None:
            current = _entry(latest)

        # **The newest revision, as a separate object rather than merged in.**
        # A board that showed the opening $40-50 band while the price walked
        # past $46 would be worse than showing nothing, because it looks
        # current. But merging the tick into the open read would publish a
        # composite no agent ever said, under the open read's byline - the same
        # objection as rewriting an estimate instead of dropping it. So both
        # travel, each attributable, and the surface decides how to show them.
        newest = log.latest("room_tick", player_key)
        if newest is not None:
            revision = _entry(newest)

    view: dict[str, Any] = {
        "current": current,
        "revision": revision,
        # **Ticks are excluded here on purpose.** There are roughly five of them
        # per nomination against one of everything else, so a straight tail of
        # the log is all ticks and nothing else - the opening reads, the
        # Strategist and the Narrator would be crowded out of the one column
        # that is supposed to show what the assistant has been saying all night.
        # The live tick is not lost; it is `revision`, which is where a running
        # price belongs.
        "recent": [_entry(r) for r in records
                   if r.agent != "room_tick"][-recent:],
        "spend_cents": log.spend_cents(),
        "timings": {a: log.timings(a) for a in log.agents() if log.timings(a)},
        "spend_dollars": round(log.spend_dollars(), 4),
        "cache_hit_rate": round(log.cache_hit_rate(), 3),
        "counts": counts,
        # Surfaced rather than hidden. A muted assistant that looks identical to
        # a quiet one is the failure this whole layer is trying not to have.
        "muted": bool(getattr(runner, "muted", False)),
        "muted_reason": getattr(runner, "muted_reason", ""),
    }
    if runner is not None:
        view["cap_dollars"] = runner.spend.cap_dollars
    return view
