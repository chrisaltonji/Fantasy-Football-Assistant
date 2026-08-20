"""Capability 6 — the ambient feed, and capability 8 riding inside it.

The dashboard shows *state*: where the board stands, always current, never
announcing. This is the other half — what **changed**, and only when the change
was worth a line. A board that re-renders silently is unreadable as history; a
feed that reports every pick is noise arriving under a clock. So every rule here
fires on a **transition**, never on a value.

**It is derived, not stored.** Nothing appends to a log. The feed is a pure
function of the journal — fold the events, compare each step to the one before,
emit where something crossed. That means it survives undo for free (the replay
filter removes the event, the transition simply never happened) and it can never
disagree with the board it sits beside, because both are computed from the same
events. No projection is stored anywhere else in this codebase and this is not
the place to start.

**The bar for a rule.** Every entry costs attention during a live auction, so a
rule earns its place only if it is (a) arithmetic over ground truth, (b) a
change rather than a standing fact, and (c) bounded — a rule that can fire on
every pick is a rule that makes the feed unreadable. `tests/unit/test_feed.py`
runs all six rules over a full 180-pick draft and asserts the totals stay small;
that test is the enforcement, not this docstring.

Prose here is deliberately flat. The inference layer will eventually say *why*
something matters; this says only what happened, in the fewest words that are
true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ffa.advice.market import market_state, value_alert
from ffa.advice.scarcity import scarcity_by_position
from ffa.domain import projections as proj
from ffa.domain.enums import Position
from ffa.domain.events import BaseEvent, PlayerSold
from ffa.domain.models import DraftState
from ffa.domain.reducers import apply, undone_ids
from ffa.reference.playerbook import PlayerBook
from ffa.util.clock import to_iso

# A rival who cannot reach this much is not competing for a starter any more.
# Used only to *count* the room, never to rule anyone out of a bid.
STILL_IN_IT = 20

# The room emptying is one story, not eleven. Reporting every decrement produced
# "2 of 11", "1 of 11", "0 of 11" on consecutive picks — technically true and
# completely unreadable. These are the fractions of the field where the shape of
# the auction actually changes, and each fires once.
ROOM_MILESTONES = (0.5, 0.25, 0.0)

# How far past the inflation-adjusted price a sale has to land before the feed
# mentions it. `value_alert.over` already applies the dynamic threshold; this
# second floor stops a $3 overpay on a $2 kicker reading like news.
NOTABLE_OVERPAY = 8

# Newest first, and capped: this is a sidebar, not an audit trail. The journal
# is the audit trail.
DEFAULT_LIMIT = 40


@dataclass(frozen=True)
class FeedEntry:
    """One thing that changed, with the numbers that made it worth saying."""

    index: int
    at: str
    channel: str          # sale | scarcity | room | plan | yours
    priority: str         # high | normal | low
    text: str

    player: str = ""
    position: str | None = None
    team: str = ""
    amount: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "at": self.at, "channel": self.channel,
            "priority": self.priority, "text": self.text,
            "player": self.player, "position": self.position,
            "team": self.team, "amount": self.amount,
        }


@dataclass
class _Watermarks:
    """What the previous step looked like, for the parts we compare.

    Only the cheap projections live here. Running the whole advisory layer at
    every one of 180 events would cost seconds; scarcity and market cost under a
    millisecond each, and they are what the transitions are about.
    """

    elite: dict[Position, int] = field(default_factory=dict)
    drying: set = field(default_factory=set)
    in_it: int | None = None
    room_hit: set = field(default_factory=set)
    plan_state: str | None = None
    seen: bool = False


def _plan_state(read) -> str | None:
    """The three-word status the plan callout shows, as a comparable token."""
    if read is None:
        return None
    if read.shortfall > 0:
        return "time to move"
    return "at risk" if read.slack < (read.bench_reserve or 0) else "on track"


def _room_still_in_it(state: DraftState) -> int:
    league = state.league
    if league is None:
        return 0
    return sum(
        1 for team_id in state.teams
        if team_id != league.my_team_id
        and proj.max_legal_bid(state, team_id) >= STILL_IN_IT
    )


def _fit_line(state: DraftState, book: PlayerBook, team_id: int, player) -> str:
    """Capability 8, in one sentence: what that pick just did to the roster.

    Deliberately reports what is *left*, not whether the buy was good. Whether
    $46 was wise is capability 11's job after the draft, and an opinion here
    would be the feed's first piece of inference.
    """
    gaps = proj.starter_gaps(state, team_id)
    remaining = sorted(f"{slot.value}×{n}" if n > 1 else slot.value
                       for slot, n in gaps.items() if n > 0)
    if not remaining:
        return "Your starting lineup is complete."
    return "Still to fill: " + ", ".join(remaining) + "."


def build_feed(
    events: Iterable[BaseEvent],
    book: PlayerBook | None,
    *,
    strategy=None,
    draft_id: str = "",
    limit: int = DEFAULT_LIMIT,
) -> tuple[FeedEntry, ...]:
    """Fold the journal into the things that changed. Newest first.

    Undone events are filtered exactly the way `replay` filters them, so an undo
    removes the transition rather than leaving a line about something that no
    longer happened.

    `strategy` is optional and is the one input that is *not* in the journal — a
    plan is declared and can be revised mid-draft, so its crossings are reported
    against the plan you hold now. Passing nothing yields a complete feed with
    no plan channel, which is the correct feed for a draft with no plan.
    """
    events = list(events)
    suppressed = undone_ids(events)

    state = DraftState.empty(draft_id)
    marks = _Watermarks()
    out: list[FeedEntry] = []
    counter = 0

    def emit(event, channel: str, priority: str, text: str, **extra) -> None:
        nonlocal counter
        out.append(FeedEntry(
            index=counter, at=to_iso(event.at) if event.at else "",
            channel=channel, priority=priority, text=text, **extra,
        ))
        counter += 1

    for event in events:
        if event.id in suppressed:
            continue
        before = state
        state = apply(state, event)
        if state.league is None:
            continue

        priced = book is not None and len(book)

        # --- a sale that went well past the room's own price ---------------
        if isinstance(event, PlayerSold) and priced:
            player = state.players.get(event.player.key)
            price = player.price.value if (player and player.price
                                           and player.price.is_known) else None
            if price is not None:
                market = market_state(before, book)     # the market *before* it
                alert = value_alert(state, book, event.player.key, price, market)
                if alert and alert.over and alert.delta >= NOTABLE_OVERPAY:
                    team = state.teams.get(
                        event.team.value if event.team and event.team.is_known else 0)
                    emit(event, "sale", "normal",
                         f"{alert.name} went ${price} against a ${alert.reference_value} "
                         f"sheet — ${alert.delta} over tonight's price.",
                         player=alert.name, team=team.label if team else "",
                         amount=price)

        # --- my own pick: what it filled, and what is left ------------------
        if isinstance(event, PlayerSold) and event.team and event.team.is_known \
                and event.team.value == state.league.my_team_id:
            player = state.players.get(event.player.key)
            price = player.price.value if (player and player.price
                                           and player.price.is_known) else None
            emit(event, "yours", "high",
                 f"You bought {event.player.raw}"
                 + (f" for ${price}. " if price is not None else ". ")
                 + _fit_line(state, book, state.league.my_team_id, player),
                 player=event.player.raw, amount=price)

        if not priced:
            continue

        # --- scarcity crossings --------------------------------------------
        scarcity = scarcity_by_position(state, book)
        for position, now in scarcity.items():
            was_elite = marks.elite.get(position)
            if marks.seen and was_elite and was_elite > 0 and now.elite == 0:
                emit(event, "scarcity", "high",
                     f"The last elite {position.value} is gone. "
                     f"{now.startable} startable left against "
                     f"{now.starting_demand} starting slots.",
                     position=position.value)
            if marks.seen and now.is_drying_up and position not in marks.drying:
                emit(event, "scarcity", "normal",
                     f"{position.value} is drying up: {now.startable} startable "
                     f"for {now.starting_demand} starting slots.",
                     position=position.value)
            marks.elite[position] = now.elite
        marks.drying = {p for p, s in scarcity.items() if s.is_drying_up}

        # --- the room thinning out ------------------------------------------
        in_it = _room_still_in_it(state)
        rivals = max(0, len(state.teams) - 1)
        if marks.seen and marks.in_it is not None and in_it < marks.in_it and rivals:
            for fraction in ROOM_MILESTONES:
                cut = int(rivals * fraction)
                if marks.in_it > cut >= in_it and fraction not in marks.room_hit:
                    marks.room_hit.add(fraction)
                    emit(event, "room", "low",
                         f"{in_it} of {rivals} rivals can still bid ${STILL_IN_IT}+."
                         + (" The auction is you and whoever is left."
                            if in_it <= 1 else ""),
                         amount=in_it)
                    break
        marks.in_it = in_it

        # --- the declared plan crossing between its three states -------------
        if strategy is not None and getattr(strategy, "is_set", False):
            from ffa.advice.strategy import strategy_read

            now = _plan_state(strategy_read(state, book, strategy))
            if now and marks.plan_state and now != marks.plan_state:
                emit(event, "plan",
                     "high" if now == "time to move" else "normal",
                     f"Your plan moved from {marks.plan_state} to {now}.")
            marks.plan_state = now or marks.plan_state

        marks.seen = True

    out.reverse()                       # newest first, as the sidebar reads
    return tuple(out[:limit])
