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


def render_guidance(guidance) -> str:
    """The readout when a player is up for bid.

    Deliberately leads with the two numbers you act on — what it's worth and
    what you can legally spend — then who can actually take him from you.
    """
    if guidance is None:
        return "no reference data loaded — advice unavailable"

    pos = f" ({guidance.position.value})" if guidance.position else ""
    lines = [f"{guidance.name}{pos}"]

    if guidance.reference_value is not None:
        inflated = ""
        if guidance.inflated_value and guidance.inflated_value != guidance.reference_value:
            inflated = f" -> ${guidance.inflated_value} at current market"
        lines.append(f"  sheet value  ${guidance.reference_value}{inflated}")
    else:
        lines.append("  sheet value  not in your reference file")

    lines.append(
        f"  bid up to    ${guidance.max_advisable_bid}"
        f"   (range ${guidance.suggested_low}-${guidance.suggested_high})"
    )
    # A trailing `+` marks the same thing it does on a rival's budget: this
    # number rests on prices we do not have yet, so it is a best case.
    ceiling = (
        f"${guidance.max_legal_bid}+ (~${guidance.safe_legal_bid} priced)"
        if guidance.ceiling_is_optimistic
        else f"${guidance.max_legal_bid}"
    )
    lines.append(
        f"  legal max    {ceiling}"
        f"   {'fills a starting slot' if guidance.fills_starter_gap else 'bench only for us'}"
    )

    live = guidance.live_threats
    if live:
        lines.append(f"  who can take him ({len(live)} live):")
        for threat in live[:6]:
            floor = "+" if threat.remaining_is_floor else ""
            lines.append(
                f"    {threat.label:<12} up to ${threat.max_legal_bid:<4} "
                f"(has ${threat.remaining}{floor}, {threat.open_at_position} slot(s))"
            )
        if len(live) > 6:
            lines.append(f"    ... and {len(live) - 6} more")
    else:
        lines.append("  who can take him: nobody who needs the position can afford him")

    if guidance.exceeds_plan_cap:
        lines.append(
            f"  plan cap     ${guidance.plan_cap}   (declared, not applied above)"
        )

    lines.extend(f"  - {reason}" for reason in guidance.reasons)

    # Their own script, where the record still separates people. `pace_reads` is
    # already empty past that point, so there is nothing to suppress here.
    notable = [r for r in guidance.pace_reads if r.is_notable][:3]
    if notable:
        seasons = max((r.seasons for r in notable), default=0)
        lines.append(f"  vs their own past drafts ({seasons} seasons):")
        for read in notable:
            delta = read.dollars_vs_script
            behind = "behind" if delta < 0 else "ahead of"
            thin = " (thin record)" if read.is_thin else ""
            lines.append(
                f"    {read.label:<12} {read.actual_share:.0%} of budget out, "
                f"usually {read.expected_share:.0%} by now"
                f"   ~${abs(delta)} {behind} script{thin}"
            )

    return "\n".join(lines)


def render_watchlist(found) -> str:
    """Capability 9. Empty is the normal answer and says so plainly.

    Ours first, because a shortfall at a position we have already filled is
    somebody else's problem and printing it under a clock is noise.
    """
    from ffa.advice.watchlist import describe

    if not found:
        return ("nothing squeezed: every position has enough left for the "
                "people who still need one.")

    mine = [s for s in found if s.mine]
    theirs = [s for s in found if not s.mine]

    lines: list[str] = []
    if mine:
        lines.append("about you:")
        for squeeze in mine:
            lines.append("  " + describe(squeeze))
            if squeeze.names:
                lines.append("    left: " + ", ".join(squeeze.names))
    if theirs:
        lines.append("elsewhere in the room:")
        lines.extend("  " + describe(s) for s in theirs)
    return "\n".join(lines)


def render_plan(read) -> str:
    """Capability 12 — the plan you wrote, against the draft you are having.

    Leads with the shortfall, because it is the only line that can tell you the
    plan is finished. Everything above it is context for that number.
    """
    if read is None:
        return (
            "no draft plan declared.\n"
            "`ffa strategy init` builds one from the market for you to edit."
        )

    floor = "+" if read.unknown_prices else ""
    lines = [
        f"plan: {read.archetype or 'custom'}",
        f"  spent        ${read.spent_total} of ${read.budget}"
        f"   (${read.remaining}{floor} left)",
    ]

    if read.shortfall:
        lines.append(
            f"  SHORT        ${read.shortfall} — the plan still calls for "
            f"${read.still_calls_for} and you have ${read.remaining}{floor}"
        )
    else:
        # "reachable", not "on track". This line is about whether the plan can
        # still be *afforded*, and a draft can be perfectly affordable while
        # being $45 under at wide receiver — which the table below says, and
        # which "on track" would talk you out of reading.
        lines.append(
            f"  reachable    plan calls for ${read.still_calls_for} more; "
            f"${read.slack} spare"
        )

    if read.max_on_one_player:
        breach = "  BREACHED" if read.breached_cap else ""
        lines.append(
            f"  one-player   cap ${read.max_on_one_player}, "
            f"biggest buy ${read.biggest_buy}{breach}"
        )

    rows = [("", "PLANNED", "SPENT", "+/-", "OPEN")]
    for position in read.positions:
        mark = "*" if position.is_material else " "
        rows.append((
            mark + position.position.value,
            f"${position.planned}",
            f"${position.spent}",
            f"{position.variance:+d}",
            str(position.open_slots),
        ))
    lines.append(_table(rows))

    if read.off_plan:
        lines.append("  * materially off plan")

    # Said once, here, rather than trusted to be remembered. Every other number
    # this tool prints is computed; these are not.
    lines.append("  (planned figures are what you declared, not what anything computed)")
    return "\n".join(lines)


def render_nomination_plan(plan) -> str:
    """The nomination readout: who is up, when you are, what to put up.

    Leads with your own next turn, because that is the only line that changes
    what you do in the next thirty seconds.
    """
    if plan is None:
        return "draft not initialized"
    if not plan.has_anything_to_say:
        return (
            "no nomination order on file.\n"
            "ESPN publishes it in draftSettings.pickOrder once the commissioner "
            "has set the draft order. `ffa config init` reads it."
        )

    lines: list[str] = []

    if plan.disagreement:
        # First, and unmissable. Everything below it is computed from an order
        # that just failed a check against the board.
        lines.append(f"! {plan.disagreement}")

    if plan.order_known:
        done, total = plan.turns_taken, plan.total_nominations
        lines.append(f"nomination {min(done + 1, total)} of {total}")

        if plan.current_nominator is not None:
            # "the order says", never "nominated by". This seat is the schedule's
            # claim, not an observation — and when the two disagree the banner
            # above already says so, so a line reading like a fact directly under
            # it would contradict the warning it is meant to support.
            lines.append(
                f"  on the block  the order says {plan.current_nominator.label} put "
                "him up"
            )

        clock = plan.on_the_clock
        if clock is None:
            lines.append("  up next       nobody — that was the last nomination")
        elif clock.is_me:
            lines.append("  up next       YOU (round "
                         f"{clock.round_number})")
        else:
            lines.append(f"  up next       {clock.label} (round {clock.round_number})")

        until = plan.nominations_until_mine
        if until is None:
            lines.append("  your turns    none left")
        elif until == 0:
            when = (
                "as soon as this one sells"
                if plan.current_nominator is not None
                else "now"
            )
            lines.append(
                f"  your turn     {when} — {plan.my_turns_left} left including this one"
            )
        else:
            plural = "" if until == 1 else "s"
            lines.append(
                f"  your turn     in {until} nomination{plural} "
                f"(round {plan.my_next.round_number}) — {plan.my_turns_left} left"
            )

        ahead = [t for t in plan.upcoming if not t.is_me][:5]
        if ahead:
            lines.append("  then          " + ", ".join(t.label for t in ahead))
    else:
        lines.append(
            "no draft order on file — cannot say whose turn it is. "
            "`ffa config init` re-reads it from ESPN."
        )

    if plan.bargains:
        shown, total = len(plan.bargains), plan.bargains_total
        more = f" (showing {shown} of {total})" if total > shown else ""
        lines.append(f"  put up to buy — nobody who needs them can pay{more}:")
        for candidate in plan.bargains:
            pos = candidate.position.value if candidate.position else "--"
            lines.append(
                f"    {candidate.name:<22} {pos:<4} ${candidate.value:<4} "
                "no live rival"
            )

    if plan.out_of_reach:
        shown, total = len(plan.out_of_reach), plan.out_of_reach_total
        more = f" (showing {shown} of {total})" if total > shown else ""
        lines.append(
            f"  past your ${plan.out_of_reach[0].my_ceiling} ceiling, and the room can "
            f"pay{more}:"
        )
        for candidate in plan.out_of_reach:
            pos = candidate.position.value if candidate.position else "--"
            lines.append(
                f"    {candidate.name:<22} {pos:<4} ${candidate.value:<4} "
                f"{candidate.live_rivals} live, up to ${candidate.contested_ceiling}"
            )

    if not plan.bargains and not plan.out_of_reach:
        lines.append(
            "  nothing to single out: no uncontested player fills a starting hole, "
            "and nothing on the board is priced past your ceiling."
            if plan.board_read
            else "  no reference data, so nothing to say about what to put up."
        )

    return "\n".join(lines)


def render_scarcity(scarcity) -> str:
    """What's left, tiered against this league's actual starting demand."""
    if not scarcity:
        return "no reference data loaded — scarcity unavailable"

    rows = [("POS", "ELITE", "STARTABLE", "BENCH", "DEMAND", "TOP LEFT", "")]
    for position, s in scarcity.items():
        rows.append((
            position.value, str(s.elite), str(s.startable), str(s.bench),
            str(s.starting_demand), f"${s.top_value_remaining}",
            "DRYING UP" if s.is_drying_up else "",
        ))
    return _table(rows)


def render_market(market) -> str:
    if market.inflation_ratio is None:
        return (
            f"market: not enough priced sales yet "
            f"({market.sales_priced} so far; ${market.dollars_spent} spent)"
        )
    return (
        f"market: {market.read} at {market.inflation_ratio:.2f}x sheet "
        f"({market.sales_priced} priced sales, ${market.dollars_spent} spent, "
        f"${market.dollars_remaining} left league-wide)"
    )


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


def describe(event: BaseEvent) -> str:
    """One human line for an event. Used by the log view and the live feed."""
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


# The live draft loop announces incoming picks with the same wording the log
# uses, so a pick reads identically whether you watched it land or scrolled
# back to it later.
_describe = describe
