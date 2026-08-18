"""Our own ceiling, when our own prices are incomplete.

`remaining_budget` charges an unknown price at the $1 minimum. That floor is
the right way to be wrong about a *rival* — it over-states their ammunition, so
no bid they make is a surprise — and the wrong way to be wrong about us.

The simulator proved it rather than argued it: with `--drop-prices 0.3` the bots
bid against that inflated figure and finished over budget once the real prices
landed. The engine flagged the overspend instead of corrupting state, which is
correct, but the bid should never have been advised.

So `safe_legal_bid` restates the same arithmetic with our unpriced picks charged
at what they most likely cost, and it — not `max_legal_bid` — is what caps
advice. `max_legal_bid` keeps its meaning; the two ceilings this codebase is
careful to distinguish do not get a third one blurred into them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.advice.bidding import guidance_for, safe_legal_bid
from ffa.advice.market import market_state
from ffa.domain import reducers
from ffa.domain.enums import Position
from ffa.domain.ids import normalize_player_key
from ffa.domain.projections import max_legal_bid
from ffa.reference.loader import LoadReport, ReferenceRow
from ffa.reference.playerbook import PlayerBook
from tests.conftest import sold

ME = 4  # `init_event`'s my_team_id


def ref(name, pos, value, rank):
    return ReferenceRow(
        key=normalize_player_key(name), name=name, position=pos,
        auction_value=float(value), overall_rank=rank,
    )


@pytest.fixture
def book() -> PlayerBook:
    rows = [
        ref("Star Back", Position.RB, 60, 1),
        ref("Costly Wideout", Position.WR, 40, 2),
        ref("Target Man", Position.WR, 30, 3),
        ref("Deep Sleeper", Position.RB, 1, 400),
    ]
    # Enough priced comparables for `market_state` to publish a ratio at all —
    # it needs five before it will, so early noise cannot swing a threshold.
    rows += [ref(f"Comp {n}", Position.WR, 10, 10 + n) for n in range(6)]
    return PlayerBook.from_report(LoadReport(source=Path("test"), rows=tuple(rows)))


def hot_market(*, multiple: float) -> list:
    """Six comparables that all sold at `multiple` times their sheet value."""
    return [
        sold(i + 20, f"Comp {i}", (i % 5) + 1, round(10 * multiple), pos=Position.WR)
        for i in range(6)
    ]


def state_of(init_event, *events):
    return reducers.replay([init_event, *events])


def guidance(state, book, key="target-man"):
    return guidance_for(state, book, key, market_state(state, book))


# --- the arithmetic ---------------------------------------------------------------


def test_with_every_price_known_the_two_ceilings_agree(init_event, book):
    """No uncertainty, nothing to be conservative about."""
    state = state_of(init_event, sold(2, "Star Back", ME, 60, pos=Position.RB))

    assert safe_legal_bid(state, book, market_state(state, book), ME) == max_legal_bid(
        state, ME
    )


def test_an_unpriced_pick_of_ours_lowers_the_safe_ceiling_by_its_sheet_value(
    init_event, book
):
    """$60 of value charged at $1 is $59 of money we do not really have."""
    state = state_of(init_event, sold(2, "Star Back", ME, None, pos=Position.RB))

    optimistic = max_legal_bid(state, ME)
    safe = safe_legal_bid(state, book, market_state(state, book), ME)

    assert optimistic - safe == 59


def test_an_unpriced_pick_with_no_sheet_value_leaves_the_ceiling_alone(
    init_event, book
):
    """There is nothing to estimate from, and inventing a number is worse.

    The $1 floor already charged for him. Guessing here would be the same
    failure in the other direction.
    """
    state = state_of(init_event, sold(2, "Nobody At All", ME, None, pos=Position.RB))

    assert safe_legal_bid(state, book, market_state(state, book), ME) == max_legal_bid(
        state, ME
    )


def test_the_estimate_follows_the_market_not_just_the_sheet(init_event, book):
    """The tool already trusts inflated value to advise a bid; use the same number.

    In a room paying 1.5x, our unpriced pick almost certainly cost 1.5x too, and
    charging him sheet value would still leave us over-funded.
    """
    state = state_of(
        init_event, *hot_market(multiple=1.5),
        sold(2, "Star Back", ME, None, pos=Position.RB),
    )
    market = market_state(state, book)
    assert market.inflation_ratio == pytest.approx(1.5)

    gap = max_legal_bid(state, ME) - safe_legal_bid(state, book, market, ME)

    # $60 on the sheet, ~$90 at this market, less the $1 already charged.
    assert gap == 89


# --- what the advice does with it ---------------------------------------------------


def test_guidance_reports_both_ceilings_and_flags_the_optimistic_one(init_event, book):
    state = state_of(init_event, sold(2, "Star Back", ME, None, pos=Position.RB))

    read = guidance(state, book)

    assert read.max_legal_bid > read.safe_legal_bid
    assert read.unknown_prices == 1
    assert read.ceiling_is_optimistic is True


def test_a_fully_priced_roster_is_not_flagged(init_event, book):
    state = state_of(init_event, sold(2, "Star Back", ME, 60, pos=Position.RB))

    read = guidance(state, book)

    assert read.ceiling_is_optimistic is False
    assert read.safe_legal_bid == read.max_legal_bid


def test_the_advisable_bid_is_capped_by_the_safe_ceiling_not_the_optimistic_one(
    init_event, book
):
    """The whole point of the fix.

    Late in a draft with an unpriced pick outstanding, the optimistic ceiling
    can sit above what we can actually pay. Advising up to it is how a roster
    finishes over budget.
    """
    # Fill our roster to one open slot, leaving one purchase unpriced.
    events = [sold(2, "Star Back", ME, None, pos=Position.RB)]
    events += [
        sold(i + 3, f"Filler {i}", ME, 1, pos=Position.RB)
        for i in range(init_event.league.draftable_slots - 2)
    ]
    state = state_of(init_event, *events)

    read = guidance(state, book)

    assert read.max_advisable_bid <= read.safe_legal_bid
    assert read.suggested_high <= read.safe_legal_bid


def test_the_caveat_is_stated_rather_than_left_to_be_inferred(init_event, book):
    """A number quietly lower than the one beside it reads as a bug."""
    state = state_of(init_event, sold(2, "Star Back", ME, None, pos=Position.RB))

    reasons = " ".join(guidance(state, book).reasons)

    assert "unpriced pick" in reasons
    assert "$1 each" in reasons


def test_a_rivals_floor_still_errs_the_other_way(init_event, book):
    """Unchanged, and deliberately so.

    Over-stating a rival's money is safe: we plan for a bid they may not be
    able to make. The two directions are not a symmetry to tidy up.
    """
    state = state_of(init_event, sold(2, "Star Back", 1, None, pos=Position.RB))

    threat = next(t for t in guidance(state, book).threats if t.team_id == 1)

    assert threat.remaining_is_floor is True
    assert threat.remaining == init_event.league.budget - 1


# --- the surfaces ------------------------------------------------------------------


def test_the_readout_marks_the_ceiling_as_a_best_case(init_event, book):
    from ffa.cli.render import render_guidance

    state = state_of(init_event, sold(2, "Star Back", ME, None, pos=Position.RB))

    text = render_guidance(guidance(state, book))

    assert "+" in text
    assert "priced" in text


def test_the_view_model_carries_the_safe_ceiling_for_every_other_surface(
    init_event, book
):
    """`build_view` is the contract the dashboard and the LLM layer read.

    Shipping only the optimistic number would push this exact bug into both.
    """
    from ffa.domain.events import PlayerNominated
    from ffa.domain.ids import PlayerRef
    from ffa.view.model import build_view
    from tests.conftest import at

    events = [
        init_event,
        sold(2, "Star Back", ME, None, pos=Position.RB),
        PlayerNominated(id=3, at=at(1), player=PlayerRef.from_raw("Target Man")),
    ]
    view = build_view(reducers.replay(events), book)

    guide = view["nomination"]["guidance"]
    assert guide["safe_legal_bid"] < guide["max_legal_bid"]
    assert guide["ceiling_is_optimistic"] is True
    assert guide["unknown_prices"] == 1
