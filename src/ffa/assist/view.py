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
    if player_key:
        latest = log.latest("room", player_key)
        if latest is not None:
            current = _entry(latest)

    view: dict[str, Any] = {
        "current": current,
        "recent": [_entry(r) for r in records[-recent:]],
        "spend_cents": log.spend_cents(),
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
