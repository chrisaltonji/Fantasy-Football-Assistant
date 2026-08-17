"""Terse commands, parsed right-anchored.

The parsing decision that matters: `sold` reads `<name...> <price> <team>` with
**price and team as the last two tokens**, so everything between the verb and
them is the player's name. Multi-word names then work with no quoting at all —
`sold patrick mahomes 45 dave` — which is what you want when the auctioneer is
already moving on.

Tokenizing uses plain `.split()`, deliberately **not** `shlex.split`, which
chokes on the apostrophe in `Ja'Marr`.

This module is pure. Everything it needs — state, the event list, the current
time — arrives in a `ParseContext`, and it returns events with `id=0` for the
store to stamp. It never reads the clock or the filesystem, so a transcript
replayed against a frozen clock produces byte-identical events.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence, Union

from ffa.domain.enums import Provenance
from ffa.domain.events import (
    AMENDABLE_FIELDS,
    FIELD_DECODERS,
    BaseEvent,
    EventUndone,
    FieldAmended,
    PlayerNominated,
    PlayerSold,
)
from ffa.domain.ids import EntityAddressError, PlayerRef, parse_entity_address
from ffa.domain.models import DraftState
from ffa.domain.sourced import known, unknown
from ffa.ingest.manual.errors import CommandError
from ffa.ingest.manual.resolve import (
    check_not_already_sold,
    looks_like_position,
    looks_like_price,
    looks_like_team,
    resolve_player,
    resolve_position,
    resolve_price,
    resolve_redo_target,
    resolve_team,
    resolve_undo_target,
)

# A `#` starts a comment only when it begins a token and isn't followed by a
# digit — otherwise it would eat the id in `undo #7`. Being able to paste a
# whole annotated session transcript back in is what lets the integration
# tests drive the REPL from a StringIO.
_COMMENT = re.compile(r"(?:^|\s)#(?!\d)")


@dataclass(frozen=True)
class ParseContext:
    """Everything the parser is allowed to know."""

    state: DraftState
    now: datetime
    events: Sequence[BaseEvent] = field(default_factory=tuple)
    # Reference data, when loaded. Absent is a supported mode — the parser
    # falls back to free-text names exactly as it did before CP3.
    book: Any = None


@dataclass(frozen=True)
class EmitCommand:
    """Events to append. Plural so a future verb can emit two atomically."""

    events: tuple[BaseEvent, ...]
    echo: str


@dataclass(frozen=True)
class ViewCommand:
    kind: str
    arg: str | None = None


@dataclass(frozen=True)
class QuitCommand:
    pass


@dataclass(frozen=True)
class NoopCommand:
    pass


Command = Union[EmitCommand, ViewCommand, QuitCommand, NoopCommand]


@dataclass(frozen=True)
class Verb:
    name: str
    aliases: tuple[str, ...]
    usage: str
    help: str


VERBS: tuple[Verb, ...] = (
    Verb("sold", ("s", "won"), "sold <player...> <price|?> <team> [pos]",
         "Record a completed sale. Price may be '?' if you don't know it yet."),
    Verb("nominate", ("nom", "n", "up"), "nominate <player...> [team]",
         "Flag the player currently up for bid."),
    Verb("price", ("p",), "price <player...> <amount>",
         "Fill in or correct a sale price."),
    Verb("amend", (), "amend <entity> <field> <value...>",
         "General correction, e.g. amend team:3 manager dave."),
    Verb("undo", ("u",), "undo [#id]",
         "Undo the last recorded event, or a specific one."),
    Verb("redo", ("r",), "redo", "Reverse the most recent undo."),
    Verb("advice", ("a",), "advice [player...]",
         "Bid readout for the nominated player, or any player you name."),
    Verb("scarcity", ("sc", "left"), "scarcity", "What's left per position, by tier."),
    Verb("market", ("mkt",), "market", "Inflation vs. your reference values."),
    Verb("budgets", ("b", "bud"), "budgets [team]", "Remaining money and max bid per team."),
    Verb("state", ("st", "board"), "state [team|player]", "Full draft board."),
    Verb("log", ("hist", "events"), "log [n]",
         "Recent events with their ids — what `undo #id` needs."),
    Verb("help", ("h", "?"), "help [verb]", "This list, or detail on one verb."),
    Verb("quit", ("q", "exit"), "quit", "Stop. Everything is already saved."),
)

BY_NAME: dict[str, Verb] = {}
for _verb in VERBS:
    BY_NAME[_verb.name] = _verb
    for _alias in _verb.aliases:
        BY_NAME[_alias] = _verb


def strip_comment(line: str) -> str:
    match = _COMMENT.search(line or "")
    return (line[: match.start()] if match else line).strip()


def parse_command(line: str, ctx: ParseContext) -> Command:
    """Parse one input line. Raises `CommandError`; never writes anything."""
    text = strip_comment(line)
    if not text:
        return NoopCommand()

    head, _, rest = text.partition(" ")
    verb = BY_NAME.get(head.lower())
    if verb is None:
        raise CommandError(_unknown_verb(head))

    return _HANDLERS[verb.name](rest.strip(), ctx, verb)


# --- verb handlers ------------------------------------------------------------


def _sold(rest: str, ctx: ParseContext, verb: Verb) -> Command:
    tokens = rest.split()
    position = None

    # An optional trailing position. Disambiguated by checking that the token
    # three from the end is price-shaped — `sold patrick mahomes 45 dave` ends
    # in a team, not a position, and price-shaped tokens are distinctive.
    if len(tokens) >= 4 and looks_like_position(tokens[-1]) and looks_like_price(tokens[-3]):
        position = resolve_position(tokens[-1])
        tokens = tokens[:-1]

    if len(tokens) < 3:
        raise CommandError(_arity(verb, len(tokens), "sold mahomes 45 dave"))

    price_token, team_token = tokens[-2], tokens[-1]
    if not looks_like_price(price_token):
        raise CommandError(
            f"{verb.usage}\nExpected a price second from the end, got {price_token!r}. "
            "Example: sold mahomes 45 dave"
        )

    name = " ".join(tokens[:-2])
    team_id = resolve_team(ctx.state, team_token)
    price = resolve_price(price_token)
    ref = resolve_player(name, ctx.book)
    check_not_already_sold(ctx.state, ref.key)

    # A position from the reference file beats nothing, but never overrides
    # what the user explicitly typed.
    if position is None and ctx.book is not None:
        row = ctx.book.get(ref.key)
        position = row.position if row else None

    event = PlayerSold(
        player=ref,
        team=known(team_id, Provenance.MANUAL, ctx.now),
        price=known(price, Provenance.MANUAL, ctx.now) if price is not None else unknown(ctx.now),
        position=known(position, Provenance.MANUAL, ctx.now) if position else None,
        note=f"sold {rest}",
    )
    shown = f"${price}" if price is not None else "$? (price unknown)"
    return EmitCommand((event,), f"{ref.raw} -> {_label(ctx.state, team_id)} {shown}")


def _nominate(rest: str, ctx: ParseContext, verb: Verb) -> Command:
    tokens = rest.split()
    if not tokens:
        raise CommandError(_arity(verb, 0, "nominate ceedee"))

    team_id = None
    if len(tokens) >= 2 and looks_like_team(ctx.state, tokens[-1]):
        team_id = resolve_team(ctx.state, tokens[-1])
        tokens = tokens[:-1]

    ref = resolve_player(" ".join(tokens), ctx.book)
    event = PlayerNominated(
        player=ref,
        nominated_by=known(team_id, Provenance.MANUAL, ctx.now) if team_id else None,
        note=f"nominate {rest}",
    )
    by = f" (by {_label(ctx.state, team_id)})" if team_id else ""
    return EmitCommand((event,), f"up for bid: {ref.raw}{by}")


def _price(rest: str, ctx: ParseContext, verb: Verb) -> Command:
    tokens = rest.split()
    if len(tokens) < 2:
        raise CommandError(_arity(verb, len(tokens), "price mahomes 45"))

    amount = resolve_price(tokens[-1])
    if amount is None:
        raise CommandError(
            "price needs an amount. To record a sale whose price you don't know, "
            "use: sold <player> ? <team>"
        )

    ref = resolve_player(" ".join(tokens[:-1]), ctx.book)
    if ref.key not in ctx.state.players:
        raise CommandError(
            f"no player {ref.raw!r} in this draft yet. Record the sale first: "
            f"sold {ref.raw} {amount} <team>"
        )

    event = FieldAmended(
        entity=f"player:{ref.key}",
        field_name="price",
        value=known(amount, Provenance.MANUAL, ctx.now),
        note=f"price {rest}",
    )
    return EmitCommand((event,), f"{ref.raw} price set to ${amount}")


def _amend(rest: str, ctx: ParseContext, verb: Verb) -> Command:
    tokens = rest.split()
    if len(tokens) < 3:
        raise CommandError(_arity(verb, len(tokens), "amend team:3 manager dave"))

    address, field_name, value_text = tokens[0], tokens[1], " ".join(tokens[2:])
    try:
        kind, _ident = parse_entity_address(address)
    except EntityAddressError as exc:
        raise CommandError(str(exc)) from None

    allowed = AMENDABLE_FIELDS.get(kind, ())
    if field_name not in allowed:
        raise CommandError(
            f"{kind} has no amendable field {field_name!r}. "
            f"Valid: {', '.join(allowed) or '(none)'}"
        )

    decoder = FIELD_DECODERS.get(field_name, str)
    try:
        value = decoder(value_text)
    except (ValueError, TypeError):
        raise CommandError(f"{value_text!r} is not a valid {field_name}") from None

    event = FieldAmended(
        entity=address, field_name=field_name,
        value=known(value, Provenance.MANUAL, ctx.now), note=f"amend {rest}",
    )
    return EmitCommand((event,), f"{address} {field_name} = {value_text}")


def _undo(rest: str, ctx: ParseContext, verb: Verb) -> Command:
    explicit = None
    token = rest.strip().lstrip("#")
    if token:
        try:
            explicit = int(token)
        except ValueError:
            raise CommandError(f"undo takes an event id like `undo #7`, got {rest!r}") from None

    target = resolve_undo_target(ctx.events, explicit)
    return EmitCommand(
        (EventUndone(target_id=target, note=f"undo {rest}".strip()),), f"undid #{target}"
    )


def _redo(rest: str, ctx: ParseContext, verb: Verb) -> Command:
    target = resolve_redo_target(ctx.events)
    return EmitCommand((EventUndone(target_id=target, note="redo"),), f"redid #{target}")


def _view(kind: str):
    def handler(rest: str, ctx: ParseContext, verb: Verb) -> Command:
        return ViewCommand(kind, rest.strip() or None)

    return handler


def _quit(rest: str, ctx: ParseContext, verb: Verb) -> Command:
    return QuitCommand()


_HANDLERS = {
    "sold": _sold,
    "nominate": _nominate,
    "price": _price,
    "amend": _amend,
    "undo": _undo,
    "redo": _redo,
    "advice": _view("advice"),
    "scarcity": _view("scarcity"),
    "market": _view("market"),
    "budgets": _view("budgets"),
    "state": _view("state"),
    "log": _view("log"),
    "help": _view("help"),
    "quit": _quit,
}


# --- error helpers ------------------------------------------------------------


def _unknown_verb(head: str) -> str:
    close = difflib.get_close_matches(head.lower(), list(BY_NAME), n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    return f"unknown command {head!r}.{hint} Type 'help' for the list."


def _arity(verb: Verb, got: int, example: str) -> str:
    return f"{verb.usage}; got {got} argument(s). Example: {example}"


def _label(state: DraftState, team_id: int | None) -> str:
    if team_id is None:
        return "?"
    team = state.teams.get(team_id)
    return team.label if team else f"team{team_id}"
