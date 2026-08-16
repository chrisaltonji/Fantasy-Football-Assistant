"""Turning state into text. The only module that formats anything.

Every function returns a string rather than printing, so output is testable
without a terminal and reusable by any surface.

Kept plain and narrow on purpose: the real draft-day display is a designed
dashboard reading the JSON view model (see `ffa/view/`), not this. What lives
here is what you need when you're typing — confirmation of what was recorded,
and the numbers on demand.
"""

from __future__ import annotations

from ffa.domain import projections as proj
from ffa.domain.enums import RosterSlot
from ffa.domain.events import (
    BaseEvent,
    DraftInitialized,
    EventUndone,
    FieldAmended,
    PlayerNominated,
    PlayerSold,
)
from ffa.domain.models import DraftState
from ffa.domain.reducers import undone_ids
from ffa.ingest.manual.grammar import VERBS, BY_NAME


def money(amount: int, *, unknowns: int = 0) -> str:
    """`$155`, or `$142+` when some prices are still unknown.

    The `+` is load-bearing: unknown prices are charged at the $1 floor, so the
    real remaining budget is *at most* this. Showing a bare number would
    present a floor as a fact.
    """
    if amount < 0:
        return f"-${abs(amount)} OVER"
    return f"${amount}" + ("+" if unknowns else "")


def render_budgets(state: DraftState, only_team: int | None = None) -> str:
    if not state.is_initialized:
        return "draft not initialized"

    rows = [("TEAM", "SPENT", "REMAINING", "MAX BID", "SLOTS", "NEEDS")]
    team_ids = [only_team] if only_team else sorted(state.teams)

    for team_id in team_ids:
        team = state.teams.get(team_id)
        if team is None:
            continue
        unknowns = proj.unknown_price_count(state, team_id)
        gaps = proj.starter_gaps(state, team_id)
        rows.append((
            ("*" if team_id == state.league.my_team_id else " ") + team.label,
            f"${proj.spent(state, team_id)}",
            money(proj.remaining_budget(state, team_id), unknowns=unknowns),
            money(proj.max_legal_bid(state, team_id)),
            f"{proj.roster_count(state, team_id)}/{state.league.draftable_slots}",
            _gap_summary(gaps),
        ))

    out = _table(rows)
    if any(proj.unknown_price_count(state, t) for t in state.teams):
        out += "\n+ = at most; some prices unknown (fill in with: price <player> <amount>)"
    return out


def render_state(state: DraftState, arg: str | None = None) -> str:
    if not state.is_initialized:
        return "draft not initialized"

    lines: list[str] = []
    nomination = state.current_nomination
    lines.append(f"up for bid: {nomination.ref.raw}" if nomination else "nothing nominated")

    sold = state.sold_players()
    lines.append(f"{len(sold)} player(s) sold")

    for team_id in sorted(state.teams):
        players = state.players_for_team(team_id)
        if not players:
            continue
        label = state.teams[team_id].label
        picks = ", ".join(f"{p.ref.raw} {_price(p)}" for p in players)
        lines.append(f"  {label}: {picks}")

    dangling = sorted(
        (p for p in state.players.values() if p.is_dangling), key=lambda p: p.ref.key
    )
    if dangling:
        # Amendments outlive an undone sale by design; showing them under their
        # own heading is what keeps that legible instead of mysterious.
        lines.append("dangling (amended but not sold — probably an undone sale):")
        lines.extend(f"  {p.ref.raw} {_price(p)}" for p in dangling)

    if state.warnings:
        lines.append(render_warnings(state.warnings))
    return "\n".join(lines)


def render_log(events, limit: int | None = None) -> str:
    """Recent events with their ids. Without this you can't discover what
    `undo #7` needs."""
    events = list(events)
    suppressed = undone_ids(events)
    shown = events[-limit:] if limit else events

    rows = [("ID", "", "EVENT")]
    for event in shown:
        rows.append((f"#{event.id}", "x" if event.id in suppressed else "", _describe(event)))
    return _table(rows)


def render_help(verb_name: str | None = None) -> str:
    if verb_name:
        verb = BY_NAME.get(verb_name.lower())
        if verb is None:
            return f"no such command {verb_name!r}"
        aliases = f"  (aliases: {', '.join(verb.aliases)})" if verb.aliases else ""
        return f"{verb.usage}{aliases}\n  {verb.help}"

    rows = [(v.usage, v.help) for v in VERBS]
    width = max(len(u) for u, _ in rows)
    return "\n".join(f"  {usage:<{width}}  {help_text}" for usage, help_text in rows)


def render_warnings(warnings) -> str:
    warnings = list(warnings)
    if not warnings:
        return ""
    return "\n".join(f"! {w}" for w in warnings)


# --- helpers ------------------------------------------------------------------


def _price(player) -> str:
    if player.price is None or not player.price.is_known:
        return "$?"
    return f"${player.price.value}"


def _gap_summary(gaps: dict[RosterSlot, int]) -> str:
    if not gaps:
        return "-"
    return " ".join(
        slot.value if count == 1 else f"{slot.value}x{count}"
        for slot, count in sorted(gaps.items(), key=lambda kv: kv[0].value)
    )


def _describe(event: BaseEvent) -> str:
    if isinstance(event, DraftInitialized):
        return f"draft started ({event.league.team_count} teams, ${event.league.budget})"
    if isinstance(event, PlayerSold):
        price = f"${event.price.value}" if event.price.is_known else "$?"
        return f"sold {event.player.raw} -> team{event.team.value} {price}"
    if isinstance(event, PlayerNominated):
        return f"nominated {event.player.raw}"
    if isinstance(event, FieldAmended):
        return f"amend {event.entity} {event.field_name} = {event.value.value}"
    if isinstance(event, EventUndone):
        return f"undo #{event.target_id}"
    return type(event).__name__


def _table(rows: list[tuple[str, ...]]) -> str:
    if not rows:
        return ""
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    )
