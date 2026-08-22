"""The Room: where each rival plausibly stops, and what is worth noticing.

Two surfaces on one call — rival estimates and pick assistance — because they
fire at the same instant on the same inputs. Splitting them would double the
latency under a bidding clock and, worse, let the halves disagree: an estimate
saying a rival stops at $40 beside a read saying he is the one to beat.

**It is a read, not a verdict.** It never says bid, pass, chase or avoid. That is
enforced three ways, and the redundancy is deliberate because prompting alone
cannot hold a property: the schema declines to offer a field that invites one,
the instructions say so in words, and `guard.check_room` rejects it mechanically
if it appears anyway. The reason it matters is placement — this text lands beside
`max_advisable_bid`, which is arithmetic, and anything printed next to a computed
number inherits its authority.

The rendered form is deliberately plain and clearly attributed. `[read]` marks
the whole block as the one part of the screen that is not measured.
"""

from __future__ import annotations

from typing import Any

from ffa.assist.guard import check_room
from ffa.assist.projection import room_payload
from ffa.assist.schemas import ROOM_SCHEMA

AGENT = "room"


def moment_key(player_key: str, event_id: int) -> str:
    """The lookup key a later agent joins on.

    Includes the event id, not just the player: a player nominated, passed in
    and re-nominated is two separate reads, and collapsing them would have the
    Narrator citing an estimate made under a different board.
    """
    return f"{AGENT}:{player_key}:{event_id}"


def threats_of(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The live threats the guard clamps estimates against.

    Straight from the deterministic guidance — this is the arithmetic the model
    is not allowed to contradict, and it is read from the view rather than
    recomputed so there is exactly one version of it.
    """
    nomination = view.get("nomination") or {}
    return list((nomination.get("guidance") or {}).get("threats") or [])


def render(parsed: dict[str, Any], *, labels: dict[int, str] | None = None) -> str:
    """The read, as it appears on the terminal.

    Returns "" when nothing survived the guard, because an empty `[read]` header
    is worse than silence: it looks like the assistant had nothing to say rather
    than like it said something inadmissible.
    """
    labels = labels or {}
    lines: list[str] = []

    read = (parsed.get("read") or "").strip()
    confidence = (parsed.get("confidence") or "").strip()
    if read:
        # The confidence travels with the sentence rather than under it. "thin"
        # is a real answer here and it needs to be impossible to miss when the
        # dossiers are empty.
        suffix = f"  ({confidence})" if confidence else ""
        lines.append(f"[read] {read}{suffix}")

    for rival in parsed.get("rivals") or []:
        team_id = rival.get("team_id")
        who = labels.get(team_id, f"team {team_id}")
        lo, hi = rival.get("lo"), rival.get("hi")
        band = f"${lo}-{hi}" if lo != hi else f"${lo}"
        why = (rival.get("rationale") or "").strip()
        lines.append(f"       {who:<14} {band:>9}   {why}")

    watch = [w for w in (parsed.get("watch_for") or []) if str(w).strip()]
    for item in watch:
        lines.append(f"       watch: {item}")

    if not lines:
        return ""
    if not read and confidence:
        lines.insert(0, f"[read] ({confidence})")
    return "\n".join(lines)


def build(view: dict[str, Any], *, prior_reads: list | None = None,
          labels: dict[int, str] | None = None) -> dict[str, Any]:
    """Everything `AssistRunner.submit` needs for one nomination.

    Assembled here rather than in the runner so the runner stays agent-agnostic,
    and assembled *now* rather than inside the worker so the payload is a frozen
    snapshot of the board at nomination time. A worker that read live state would
    describe a board that had already moved on.
    """
    nomination = view.get("nomination") or {}
    player_key = nomination.get("player_key") or nomination.get("key") or ""
    threats = threats_of(view)

    return {
        "agent": AGENT,
        "payload": room_payload(view, prior_reads=prior_reads),
        "player_key": player_key,
        "schema": ROOM_SCHEMA,
        # Both closures capture only what is already computed. `check` runs on
        # the worker, `show` on the main thread, and neither reads the store.
        "check": lambda parsed: check_room(parsed, threats),
        "show": lambda parsed: render(parsed, labels=labels),
    }
