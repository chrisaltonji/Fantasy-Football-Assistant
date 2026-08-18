"""Capability 4 — whose turn it is to nominate, and what the board says to put up.

ESPN publishes the entire nomination order *before the draft starts*, in
`draftSettings.pickOrder`, and this codebase read past it for its whole life.
That single list is the difference between "a player appeared, react" and
"you nominate third from now, and here is what is worth putting up".

**The order is a cycle, and that is measured rather than assumed.** All 180
`nominatingTeamId`s of a completed auction — and of every pre-draft skeleton in
`tests/fixtures/espn/` — are the twelve-seat `pickOrder` repeated fifteen times.
No snake, no rotation. `test_nomination.py` asserts it against the real fixture,
so if ESPN ever changes that, a test fails rather than a readout lying.

Two things this module refuses to do:

- **Fall back to team-id order.** ESPN's order is a shuffle (`orderType:
  MANUAL`), so id order is not a degraded answer, it is a wrong one — it would
  name a specific manager as on the clock and be confidently incorrect about a
  fact the user can see on their own screen. No order means no readout.
- **Recommend which name to put up.** That depends on a strategy — drain the
  room, chase your guys, hoard — and the strategy preset is C2, which does not
  exist. What ships instead are the two candidate lists that need no preference
  to justify; see `NominationPlan`.
"""

from __future__ import annotations

from ffa.advice.bidding import has_starter_gap, safe_legal_bid, threats_for
from ffa.advice.market import inflated_value
from ffa.advice.types import (
    MarketState,
    NominationCandidate,
    NominationPlan,
    NominationTurn,
    Threat,
)
from ffa.domain.enums import Position
from ffa.domain.models import DraftState
from ffa.reference.playerbook import PlayerBook

# How many turns ahead to name, and how many candidates to list. Both lists
# carry their untruncated total alongside, because a list that silently stops at
# five reads as "there are five" — and the counts are the part you act on.
UPCOMING_TURNS = 6
CANDIDATE_LIMIT = 5

# The least a player has to be worth before putting him up counts as draining
# anybody. Without it the list degenerates exactly when it is least useful: late
# in a draft our own ceiling reaches $0, every remaining $1 body is technically
# "priced past" it, and a 180-pick dry run duly offered 202 of them. Draining a
# rival of one dollar is not a strategy, it is noise arriving under a clock.
MIN_DRAIN = 5


def turns_taken(state: DraftState) -> int:
    """How many nominations have already resolved.

    Sales are the anchor, and the 1:1 that makes them one is measured: the
    completed-auction fixture has 180 picks, all filled, every `bidAmount` at
    least $1. An ESPN nomination cannot go unsold — the nominator opens the
    bidding themselves — so the Nth sale is the Nth resolved nomination.

    It deliberately does not count `PlayerNominated` events instead. The live
    reader emits one per player it sees on the block, so a re-render that
    re-observes the same nomination would advance a counter that sales cannot
    double-count.

    Where this *can* drift is the simulator, which does let a nomination fizzle
    when nobody left can buy — a 180-sale dry run took 194 nominations to get
    there. That is the sim modelling something ESPN does not do, and
    `NominationPlan.disagreement` catches it rather than the schedule quietly
    sliding a seat. It is what the banner is for.
    """
    return len(state.sold_players())


def turn_at(state: DraftState, index: int) -> NominationTurn | None:
    """The seat nominating at 0-based `index`, or `None` if unknowable."""
    league = state.league
    if league is None:
        return None
    team_id = league.nominator_at(index)
    if team_id is None:
        return None

    team = state.teams.get(team_id)
    per_round = len(league.nomination_order) or league.team_count or 1
    return NominationTurn(
        index=index,
        round_number=(index // per_round) + 1,
        team_id=team_id,
        label=team.label if team is not None else f"team{team_id}",
        is_me=team_id == league.my_team_id,
    )


def _candidates(
    state: DraftState, book: PlayerBook, market: MarketState
) -> tuple[
    tuple[NominationCandidate, ...], tuple[NominationCandidate, ...], int, int
]:
    """Split the remaining board into the two lists arithmetic can defend.

    Rival capacity is computed **once per position, not once per player**: a
    `Threat` depends on the position and the rival's roster, never on which
    player at that position is under discussion. Doing it per player would run
    the whole projection layer a few hundred times for identical answers.
    """
    league = state.league
    if league is None or not len(book):
        return (), (), 0, 0

    me = league.my_team_id
    my_ceiling = safe_legal_bid(state, book, market, me)

    taken = {p.ref.key for p in state.sold_players()}
    if state.current_nomination is not None:
        # Already on the block. Whatever it is, it is not a name to put up.
        taken.add(state.current_nomination.ref.key)

    per_position: dict[Position, tuple[tuple[Threat, ...], bool]] = {}

    def read_position(position: Position) -> tuple[tuple[Threat, ...], bool]:
        if position not in per_position:
            per_position[position] = (
                tuple(t for t in threats_for(state, position, exclude_team=me) if t.is_live),
                has_starter_gap(state, me, position),
            )
        return per_position[position]

    bargains: list[NominationCandidate] = []
    out_of_reach: list[NominationCandidate] = []

    for position in Position:
        live, my_gap = read_position(position)
        contested_ceiling = max((t.max_legal_bid for t in live), default=0)

        for row in book.by_position(position):
            if row.key in taken:
                continue
            value = inflated_value(book, row.key, market) or book.value(row.key)
            if value is None:
                # No reference row. Every test below is a comparison against a
                # price, and inventing one is the failure this codebase refuses
                # everywhere else.
                continue

            candidate = NominationCandidate(
                key=row.key,
                name=row.name,
                position=position,
                value=int(value),
                live_rivals=len(live),
                contested_ceiling=contested_ceiling,
                fills_my_starter_gap=my_gap,
                my_ceiling=my_ceiling,
            )

            # Free money: we need him, and nobody who could start him can pay.
            if my_gap and not live and value <= my_ceiling:
                bargains.append(candidate)
            # Out of our hands: *he* costs more than we can pay, and a live rival
            # can actually pay it. Putting him up cannot cost us a player we
            # could have won, because we could not have won him.
            #
            # Both halves are about this player's own price, and that is the
            # point. The tempting test — "a rival's ceiling beats ours" — has no
            # player in it at all: every name at a position shares one
            # `contested_ceiling` and one `my_ceiling`, so it returns the entire
            # remaining board or none of it. Measured at 60 sales into a real
            # draft it returned 292 of 292 remaining players, which is not a
            # shortlist, it is a copy of the board.
            elif (
                live
                and value >= MIN_DRAIN
                and value > my_ceiling
                and contested_ceiling >= value
            ):
                out_of_reach.append(candidate)

    bargains.sort(key=lambda c: (-c.value, c.name))
    # Priciest first: every one of these is already within a live rival's reach,
    # so the player's own price is the money that leaves the room.
    out_of_reach.sort(key=lambda c: (-c.value, c.name))

    return (
        tuple(bargains[:CANDIDATE_LIMIT]),
        tuple(out_of_reach[:CANDIDATE_LIMIT]),
        len(bargains),
        len(out_of_reach),
    )


def nomination_plan(
    state: DraftState, book: PlayerBook | None, market: MarketState | None = None
) -> NominationPlan | None:
    """The whole capability-4 read. `None` before the draft is initialized.

    Returns a plan with `order_known=False` rather than `None` when the order is
    missing but the board is not, because the candidate lists are still worth
    something without it — you can know what is worth putting up without knowing
    exactly when your turn lands.
    """
    league = state.league
    if league is None:
        return None

    taken = turns_taken(state)
    total = league.total_nominations
    nominated = state.current_nomination

    # The seat on the clock is the one that must make the *next* nomination. When
    # a player is already up, that nomination has been made and belongs to the
    # previous index — conflating the two is how a readout tells you it is your
    # turn while somebody else's player is on the block.
    current_nominator = turn_at(state, taken) if nominated is not None else None
    next_index = taken + (1 if nominated is not None else 0)
    on_the_clock = turn_at(state, next_index)

    my_next = None
    my_turns_left = 0
    if league.nomination_order and total:
        for index in range(next_index, total):
            turn = turn_at(state, index)
            if turn is not None and turn.is_me:
                my_turns_left += 1
                if my_next is None:
                    my_next = turn

    # The board is the truth and the schedule is a model of it. Say so when they
    # part company rather than continuing to name a seat.
    disagreement = ""
    if (
        nominated is not None
        and current_nominator is not None
        and nominated.nominated_by is not None
        and nominated.nominated_by.is_known
        and nominated.nominated_by.value != current_nominator.team_id
    ):
        observed = state.teams.get(nominated.nominated_by.value)
        observed_label = (
            observed.label if observed is not None
            else f"team{nominated.nominated_by.value}"
        )
        disagreement = (
            f"the board credits this nomination to {observed_label}, but the "
            f"draft order says {current_nominator.label} was up at #{taken + 1}. "
            "Trust the board — the order this tool holds is stale or wrong, and "
            "`ffa config init` re-reads it."
        )

    bargains: tuple[NominationCandidate, ...] = ()
    out_of_reach: tuple[NominationCandidate, ...] = ()
    bargains_total = out_of_reach_total = 0
    board_read = book is not None and bool(len(book))
    if board_read:
        bargains, out_of_reach, bargains_total, out_of_reach_total = _candidates(
            state, book, market or MarketState()
        )

    return NominationPlan(
        order_known=bool(league.nomination_order),
        turns_taken=taken,
        total_nominations=total,
        on_the_clock=on_the_clock,
        upcoming=tuple(
            turn
            for turn in (
                turn_at(state, i) for i in range(next_index, next_index + UPCOMING_TURNS)
            )
            if turn is not None
        ),
        current_nominator=current_nominator,
        my_next=my_next,
        my_turns_left=my_turns_left,
        bargains=bargains,
        out_of_reach=out_of_reach,
        bargains_total=bargains_total,
        out_of_reach_total=out_of_reach_total,
        board_read=board_read,
        disagreement=disagreement,
    )
