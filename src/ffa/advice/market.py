"""Market inflation: are people paying over or under the sheet?

Deterministic arithmetic only — actual dollars paid divided by reference value
for the same players. The qualitative read ("the room is hot, expect the next
tier to go over") is the Claude layer's job; this just supplies the ratio it
reasons from.

Feeds the dynamic overspend threshold (capability 5), the bid recommendation
(capability 3), and nomination strategy (capability 4).
"""

from __future__ import annotations

from ffa.advice.types import MarketState, ValueAlert
from ffa.domain.models import DraftState
from ffa.reference.playerbook import PlayerBook

# How far past the inflation-adjusted value a sale has to land before it counts
# as an overpay. Relative to the *current* market, per the capability spec's
# requirement that the threshold be dynamic rather than a fixed percentage.
OVERPAY_MARGIN = 0.25


def market_state(state: DraftState, book: PlayerBook) -> MarketState:
    league = state.league
    if league is None:
        return MarketState()

    spent = 0
    reference = 0
    priced = 0

    for player in state.sold_players():
        if player.price is None or not player.price.is_known:
            continue
        value = book.value(player.ref.key)
        if value is None:
            # No reference for this player — count the dollars, but leaving him
            # out of the denominator keeps the ratio honest.
            spent += player.price.value
            continue
        priced += 1
        spent += player.price.value
        reference += value

    total_money = league.budget * league.team_count
    # Needs a real sample before the ratio means anything; early noise would
    # swing every downstream threshold.
    ratio = (spent / reference) if (reference > 0 and priced >= 5) else None

    return MarketState(
        sales_priced=priced,
        dollars_spent=spent,
        reference_spent=reference,
        dollars_remaining=total_money - spent,
        inflation_ratio=round(ratio, 3) if ratio is not None else None,
    )


def inflated_value(book: PlayerBook, key: str, market: MarketState) -> int | None:
    """A player's reference value restated at current market temperature."""
    value = book.value(key)
    if value is None:
        return None
    ratio = market.inflation_ratio or 1.0
    return max(1, round(value * ratio))


def value_alert(
    state: DraftState, book: PlayerBook, key: str, price: int, market: MarketState
) -> ValueAlert | None:
    """Did this sale cross the dynamic overspend threshold?

    Every finalized sale is evaluated league-wide, not just ones we bid on —
    the log of price-vs-value is what feeds the inflation tracker.
    """
    reference = book.value(key)
    if reference is None:
        return None

    adjusted = inflated_value(book, key, market) or reference
    player = state.players.get(key)
    return ValueAlert(
        key=key,
        name=player.ref.raw if player else key,
        price=price,
        reference_value=reference,
        inflated_value=adjusted,
        delta=price - adjusted,
        over=price > adjusted * (1 + OVERPAY_MARGIN),
    )
