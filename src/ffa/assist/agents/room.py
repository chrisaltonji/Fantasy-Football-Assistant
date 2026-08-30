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

## Two gears

The **open** read fires once, when the player goes up, on the full board. The
**tick** fires every ~5s while the bidding runs, on a small payload and a fast
model, and is a *revision of the open read* rather than a fresh analysis.

They are one agent in one module on purpose. It is one voice about one player,
sharing a guard, a rendering vocabulary and a moment key — and the whole premise
of the tick is that it is answering back to something this same module already
said. Two modules would have made that a cross-agent join rather than a local one.

What differs between them is a profile row and a payload, which is exactly the
amount of difference there actually is.
"""

from __future__ import annotations

from typing import Any

from ffa.assist.guard import check_room, check_room_tick
from ffa.assist.projection import room_payload, room_tick_payload
from ffa.assist.schemas import ROOM_SCHEMA, ROOM_TICK_SCHEMA

AGENT = "room"
TICK_AGENT = "room_tick"


def moment_key(player_key: str, event_id: int) -> str:
    """The lookup key a later agent joins on.

    Includes the event id, not just the player: a player nominated, passed in
    and re-nominated is two separate reads, and collapsing them would have the
    Narrator citing an estimate made under a different board.
    """
    return f"{AGENT}:{player_key}:{event_id}"


def tick_moment_key(player_key: str, event_id: int, price: int) -> str:
    """The same, plus the price, because there are many ticks per nomination.

    The price rather than a counter: two ticks at the same price on the same
    nomination genuinely *are* the same moment, and a counter would file them as
    two — which would make the sidecar read as though the board had moved when it
    had not. It also means the key states what was true when the read was made,
    which is the property that makes the log worth reading back.
    """
    return f"{TICK_AGENT}:{player_key}:{event_id}:{price}"


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


def _bands(parsed: dict[str, Any]) -> dict[int, tuple[int, int]]:
    """Rival bands as a comparable mapping, for deciding whether a tick is news."""
    out: dict[int, tuple[int, int]] = {}
    for rival in parsed.get("rivals") or []:
        try:
            out[int(rival["team_id"])] = (int(rival["lo"]), int(rival["hi"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def render_tick(parsed: dict[str, Any], *, last: dict[str, Any] | None = None,
                crossed: str = "", labels: dict[int, str] | None = None) -> str:
    """One line, or nothing — and nothing is the expected answer.

    **Silence is the default and it is the feature.** This fires every five
    seconds. A line per tick is roughly forty lines while one player is on the
    block, which does not read as an assistant paying attention; it reads as
    noise scrolling the arithmetic off the screen at the exact moment someone is
    trying to decide whether to raise. The deterministic readout is what must
    stay visible, and this is the only thing on screen that can push it away.

    So three gates, in order:

    - the model said nothing changed — the common case, by design;
    - nothing survived the guard;
    - it changed its mind but says what it last said, which happens whenever the
      price moves inside a band it has already given.

    `crossed` overrides the last gate, and it means *just crossed*. The price
    passing our own ceiling is worth saying again even verbatim, because the same
    sentence means something different on the other side of that number.

    **It is the transition, not the state.** A live rehearsal printed this three
    times in one auction, twice near-identically, because the price stayed above
    the ceiling and so every remaining tick claimed to be news. Once past, it is
    past; the caller only passes a label the first time. See `_maybe_tick`.
    """
    if not parsed.get("changed"):
        return ""

    labels = labels or {}
    note = (parsed.get("note") or "").strip()
    bands = _bands(parsed)
    if not note and not bands:
        return ""

    if last is not None and not crossed:
        # Saying the same thing again is not news. Compared on content rather
        # than on the whole reply, so a changed confidence or a dropped
        # rationale does not by itself count as movement.
        if note == (last.get("note") or "").strip() and bands == _bands(last):
            return ""

    head = f"[read] {note}" if note else "[read]"
    if crossed:
        head += f"  — past {crossed}"
    confidence = (parsed.get("confidence") or "").strip()
    if confidence:
        head += f"  ({confidence})"

    lines = [head]
    for team_id, (lo, hi) in bands.items():
        who = labels.get(team_id, f"team {team_id}")
        band = f"${lo}-{hi}" if lo != hi else f"${lo}"
        lines.append(f"       {who:<14} {band:>9}   now")
    return "\n".join(lines)


def crossing(live_bid: dict[str, Any] | None, view: dict[str, Any]) -> str:
    """Which of our own computed ceilings the price is above, if any.

    Arithmetic, done here on the main thread and handed to the renderer as a
    finding. The model is never asked whether the price passed a number — it is
    shown the numbers and the price, and this decides. Same division as
    everywhere else in the package: the model says what something means, never
    whether it happened.

    **This reports a state, not an event.** It keeps answering "your plan cap"
    for every tick while the price stays above it. Turning that into "has just
    crossed" needs memory of what was already reported, which lives on the live
    board — see `_Bidding.newly_crossed`.
    """
    price = (live_bid or {}).get("price")
    if not isinstance(price, int):
        return ""

    guidance = (view.get("nomination") or {}).get("guidance") or {}
    # Ordered by which is the more serious thing to have passed, so a price above
    # both names the one that actually binds.
    for label, key in (("your plan cap", "plan_cap"),
                       ("the advisable bid", "max_advisable_bid")):
        value = guidance.get(key)
        if isinstance(value, int) and price > value:
            return label
    return ""


def build(view: dict[str, Any], *, prior_reads: list | None = None,
          digest: str = "", labels: dict[int, str] | None = None
          ) -> dict[str, Any]:
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
        "payload": room_payload(view, prior_reads=prior_reads, digest=digest),
        "player_key": player_key,
        "schema": ROOM_SCHEMA,
        # Both closures capture only what is already computed. Neither reads the
        # store, and neither can see a board that has moved since the spawn.
        "check": lambda parsed: check_room(parsed, threats),
        "show": lambda parsed: render(parsed, labels=labels),
    }


def build_tick(view: dict[str, Any], *, live_bid: dict[str, Any] | None = None,
               opening_read: dict[str, Any] | None = None,
               last_tick: dict[str, Any] | None = None, crossed: str = "",
               digest: str = "", labels: dict[int, str] | None = None
               ) -> dict[str, Any]:
    """The same, for one tick of live bidding.

    `last_tick` is the previously *shown* tick, captured at spawn like everything
    else here. It is what `render_tick` suppresses against. Freezing it at spawn
    rather than reading it when the reply lands is the same argument as the rest
    of this file: two ticks can be in flight across a slow API, and a renderer
    that read live state would compare against whichever happened to land first.

    `crossed` is passed in rather than computed here, and it is a *transition*:
    the caller owns the memory of which ceilings have already been reported, so
    this stays a pure function of its arguments like everything else in the file.
    """
    nomination = view.get("nomination") or {}
    player_key = nomination.get("player_key") or nomination.get("key") or ""
    threats = threats_of(view)

    return {
        "agent": TICK_AGENT,
        "payload": room_tick_payload(
            view, live_bid=live_bid, opening_read=opening_read, digest=digest,
        ),
        "player_key": player_key,
        "schema": ROOM_TICK_SCHEMA,
        "check": lambda parsed: check_room_tick(parsed, threats),
        "show": lambda parsed: render_tick(
            parsed, last=last_tick, crossed=crossed, labels=labels,
        ),
    }
