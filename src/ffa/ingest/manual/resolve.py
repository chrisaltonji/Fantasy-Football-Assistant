"""Turning what the user typed into things the domain recognizes.

Pure: reads state, never touches the clock or the filesystem. Every function
here either returns a resolved value or raises `CommandError` with a message
that names the alternatives.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from ffa.domain.enums import Position
from ffa.domain.events import BaseEvent, DraftInitialized, EventUndone
from ffa.domain.models import DraftState
from ffa.domain.reducers import undone_ids
from ffa.ingest.manual.errors import CommandError

_SLOT_TOKEN = re.compile(r"^(?:t|team)?(\d+)$", re.IGNORECASE)
_PRICE_TOKEN = re.compile(r"^\$?(\d+)$")

UNKNOWN_PRICE_TOKENS = {"?", "-", "unknown"}


# --- teams --------------------------------------------------------------------


def resolve_team(state: DraftState, token: str) -> int:
    """`t3` / `team3` / `3` / `dave` / `dav` -> ESPN team id.

    Slot forms are tried first so a manager unwise enough to nickname
    themselves "3" can't shadow team 3.
    """
    text = (token or "").strip()
    if not text:
        raise CommandError("no team given")

    slot = _SLOT_TOKEN.match(text)
    if slot:
        team_id = int(slot.group(1))
        if team_id in state.teams:
            return team_id
        raise CommandError(
            f"no team {team_id} in this league (teams are 1-{len(state.teams)})"
        )

    lowered = text.lower()
    exact = [t for t in state.teams.values() if lowered in _team_aliases(t)]
    if len(exact) == 1:
        return exact[0].team_id
    if len(exact) > 1:  # pragma: no cover - config validation forbids duplicates
        raise CommandError(f"{text!r} matches more than one team")

    prefixed = [t for t in state.teams.values() if any(a.startswith(lowered) for a in _team_aliases(t))]
    if len(prefixed) == 1:
        return prefixed[0].team_id
    if len(prefixed) > 1:
        names = ", ".join(sorted(t.label for t in prefixed))
        raise CommandError(f"{text!r} is ambiguous — matches {names}")

    raise CommandError(
        f"no team matches {text!r}. Known: {_team_list(state)}\n"
        "Name one with: amend team:3 manager dave"
    )


def _team_aliases(team) -> set[str]:
    aliases = set()
    for sourced in (team.manager, team.name):
        if sourced is not None and sourced.is_known:
            aliases.add(str(sourced.value).lower())
    return aliases


def _team_list(state: DraftState) -> str:
    labels = [f"{t.team_id}:{t.label}" for t in sorted(state.teams.values(), key=lambda t: t.team_id)]
    return ", ".join(labels) if labels else "(none)"


def looks_like_team(state: DraftState, token: str) -> bool:
    """Non-raising probe, for deciding whether a trailing token is a team."""
    try:
        resolve_team(state, token)
    except CommandError:
        return False
    return True


# --- prices and positions -----------------------------------------------------


def resolve_price(token: str) -> int | None:
    """Whole dollars, or `None` for an explicit "sold, price unknown"."""
    text = (token or "").strip().lower()
    if text in UNKNOWN_PRICE_TOKENS:
        return None
    match = _PRICE_TOKEN.match(text)
    if not match:
        raise CommandError(
            f"price must be whole dollars or '?' for unknown, got {token!r}. "
            "Example: sold mahomes 45 dave"
        )
    return int(match.group(1))


def looks_like_price(token: str) -> bool:
    text = (token or "").strip().lower()
    return text in UNKNOWN_PRICE_TOKENS or bool(_PRICE_TOKEN.match(text))


def resolve_position(token: str) -> Position:
    try:
        return Position.parse(token)
    except ValueError:
        raise CommandError(
            f"{token!r} is not a position. Valid: {', '.join(p.value for p in Position)}"
        ) from None


def looks_like_position(token: str) -> bool:
    try:
        Position.parse(token)
    except ValueError:
        return False
    return True


# --- players ------------------------------------------------------------------


def resolve_player(text: str, book=None) -> "PlayerRef":
    """Identify a typed name against the reference data.

    Three outcomes, and the middle one is the point:

    - **Confident match** → the *canonical* key from the reference file, with
      what you typed kept in `raw`. Using the canonical key is what stops
      `bramford` and `aaron bramford` becoming two separate players. The key is
      still fixed at write time and never recomputed, so journals stay
      self-describing and replay without the reference file.
    - **Ambiguous** → refuse, and list the candidates. Never auto-accept: a
      wrong match wears a real player's price and fails silently, which is the
      worst failure this tool has.
    - **No match at all** → accept as free text. A player missing from the
      sheet is normal (deep sleepers, late adds); the tool must not block on it.
    """
    from ffa.domain.ids import PlayerRef

    typed = (text or "").strip()
    if not typed:
        raise CommandError("no player name given")
    if book is None or not len(book):
        return PlayerRef.from_raw(typed)

    resolution = book.resolve(typed)
    if resolution.resolved:
        row = resolution.match.row
        # `raw` becomes the canonical name, not the two letters you typed —
        # it is the display name everywhere downstream, and "Halson" reads as
        # a mystery in a readout. The literal input isn't lost: the event's
        # `note` carries the whole command line.
        return PlayerRef(key=row.key, raw=row.name, player_id=row.espn_player_id)

    if resolution.is_ambiguous:
        options = "\n".join(
            f"  {n}) {m.row.name} ({m.row.position.value}"
            + (f", {m.row.nfl_team}" if m.row.nfl_team else "")
            + f") — {m.confidence:.0%}"
            for n, m in enumerate(resolution.candidates, 1)
        )
        raise CommandError(
            f"{typed!r} could be more than one player:\n{options}\n"
            "Type more of the name to pick one."
        )

    return PlayerRef.from_raw(typed)


def check_not_already_sold(state: DraftState, key: str) -> None:
    """Stop a *human* creating an ambiguous double entry.

    Command-layer policy only. When CP5's poller reports a sale you already
    typed, it must merge rather than be refused — the reducer handles that
    idempotently, and this check never runs on it.
    """
    player = state.players.get(key)
    if player is None or not player.sold:
        return

    where = ""
    if player.team is not None and player.team.is_known:
        team_id = player.team.value
        label = state.teams[team_id].label if team_id in state.teams else f"team{team_id}"
        price = player.price.value if player.price is not None and player.price.is_known else "?"
        where = f" to {label} for ${price}"

    raise CommandError(
        f"{player.ref.raw!r} is already sold{where}.\n"
        f"To fix the price: price {key} <amount>   To remove it: log, then undo #<id>"
    )


# --- undo / redo targets -------------------------------------------------------


def resolve_undo_target(events: Sequence[BaseEvent], explicit: int | None = None) -> int:
    """Which event a bare `undo` (or `undo #7`) should suppress."""
    suppressed = undone_ids(events)

    if explicit is not None:
        event = _find(events, explicit)
        if event is None:
            raise CommandError(f"no event #{explicit} in this draft. Try: log")
        if isinstance(event, DraftInitialized):
            raise CommandError(
                "cannot undo draft initialization — delete the run directory instead"
            )
        if explicit in suppressed:
            raise CommandError(f"event #{explicit} is already undone. To bring it back: redo")
        return explicit

    for event in reversed(list(events)):
        # Skipping EventUndone here is what makes two bare undos remove the two
        # most recent picks, instead of toggling one pick on and off.
        if isinstance(event, (EventUndone, DraftInitialized)):
            continue
        if event.id not in suppressed:
            return event.id

    raise CommandError("nothing to undo")


def resolve_redo_target(events: Sequence[BaseEvent]) -> int:
    """The most recent undo that is still in effect."""
    suppressed = undone_ids(events)
    for event in reversed(list(events)):
        if isinstance(event, EventUndone) and event.id not in suppressed:
            return event.id
    raise CommandError("nothing to redo")


def _find(events: Iterable[BaseEvent], event_id: int) -> BaseEvent | None:
    return next((e for e in events if e.id == event_id), None)
