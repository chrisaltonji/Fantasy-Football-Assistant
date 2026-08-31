"""What we said, against what happened.

The Narrator's best line is "he went $47, eight over what we expected" — and the
subtraction in it is arithmetic. So we do it, here, on the main thread, and hand
the result over as a fact. Asking a model to compute a delta it could have been
given is how a system starts disagreeing with itself in prose.

Also the Grader's raw material: every read joined to its outcome is how the
assistant gets scored on its own calls rather than on how it sounded.
"""

from __future__ import annotations

from typing import Any


def compare(read: dict[str, Any] | None, *, price: int | None,
            winner_team_id: int | None) -> dict[str, Any]:
    """Join one Room read to the sale that resolved it.

    `band` is the whole point: `under` / `inside` / `over` is the single word a
    later agent needs, and deriving it here means every surface says the same
    thing about the same call.
    """
    out: dict[str, Any] = {
        "had_read": bool(read),
        "actual": price,
        "winner_team_id": winner_team_id,
        "expected_lo": None,
        "expected_hi": None,
        "delta": None,
        "band": "unknown",
        "winner_was_named": False,
        "winner_expected_hi": None,
    }
    if not read or price is None:
        return out

    rivals = [r for r in (read.get("rivals") or []) if isinstance(r, dict)]
    if not rivals:
        return out

    # The room's expectation is the span the named rivals covered — the lowest
    # anybody was expected to stop, and the highest.
    lows = [int(r["lo"]) for r in rivals if "lo" in r]
    highs = [int(r["hi"]) for r in rivals if "hi" in r]
    if not lows or not highs:
        return out

    lo, hi = min(lows), max(highs)
    out["expected_lo"], out["expected_hi"] = lo, hi
    out["delta"] = price - hi if price > hi else (price - lo if price < lo else 0)
    out["band"] = "over" if price > hi else "under" if price < lo else "inside"

    for rival in rivals:
        if winner_team_id is not None and int(rival.get("team_id", -1)) == winner_team_id:
            out["winner_was_named"] = True
            out["winner_expected_hi"] = int(rival.get("hi", 0))
            break
    return out
