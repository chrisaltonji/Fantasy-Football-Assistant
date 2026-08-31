"""Capability 12 — what you said you would do, against what you have done.

The arithmetic half of C2. The plan itself is testimony and lives in
`config/strategy.py`; everything here is computed from the board, and the two
are kept in separate modules so it stays obvious which is which.

**This never touches a bid.** `max_on_one_player` is a preference, and capping
`max_advisable_bid` with it would quietly redefine a number the market
computed — the readout would say a player is worth $58 because you *said* $58,
which is the failure this codebase avoids everywhere else. It rides alongside
instead, exactly the way `PaceRead` does, and there is a test that says deleting
every strategy read leaves the bid arithmetic unchanged.

The load-bearing number here is `shortfall`: the dollars your plan still calls
for, minus the dollars you have. Positive means the plan is no longer reachable,
and knowing that at pick 60 is the whole point of having written one down.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ffa.config.strategy import StrategyPreset
from ffa.domain import projections as proj
from ffa.domain.enums import Position, RosterSlot
from typing import Mapping
from ffa.domain.models import DraftState

# How far off plan a position has to be before it is worth a line. Below this it
# is rounding, and a readout that flags every $3 wobble is one you stop reading.
MATERIAL = 5


@dataclass(frozen=True)
class PositionAdherence:
    """One position: planned, spent, and whether we are still buying there."""

    position: Position
    planned: int
    spent: int
    open_slots: int

    @property
    def variance(self) -> int:
        """Positive means overspent against the plan."""
        return self.spent - self.planned

    @property
    def still_calls_for(self) -> int:
        """Plan dollars not yet spent, where there is still somewhere to put them.

        A position we have finished buying releases its underspend rather than
        keeping it earmarked — otherwise a cheap tight end would show as a
        permanent $8 hole in a plan that is, in fact, complete.
        """
        if self.open_slots <= 0:
            return 0
        return max(0, self.planned - self.spent)

    @property
    def is_material(self) -> bool:
        """Worth flagging — and the two directions are not symmetric.

        **Overspending counts immediately.** Being $20 past your running-back
        budget is a fact about money already gone, and it is exactly as true at
        pick 12 as at pick 180.

        **Underspending only counts once you have finished buying there.** With
        slots still open, "under plan" is not a departure from the plan, it is
        the plan not having happened yet — and a readout that flags it says
        every position is off plan before you have bought a single player, which
        is what a full dry run duly showed at pick 0.
        """
        if self.variance >= MATERIAL:
            return True
        return self.variance <= -MATERIAL and self.open_slots <= 0


@dataclass(frozen=True)
class StrategyRead:
    """Adherence to the declared plan, as of right now."""

    archetype: str = ""
    budget: int = 0
    planned_total: int = 0
    bench_reserve: int = 0
    spent_total: int = 0
    remaining: int = 0
    # `remaining` charges our own unpriced picks at the $1 minimum, so it is a
    # best case — the same `+` convention every other budget in this codebase
    # uses. A shortfall computed from it is therefore the *optimistic* one.
    unknown_prices: int = 0

    positions: tuple[PositionAdherence, ...] = ()
    # The plan as it was actually declared: dollars per starting slot, keyed
    # "QB", "RB1", ... "FLEX". Carried verbatim rather than recomputed - it is
    # the one number here that was typed rather than derived, and `positions`
    # above is its sum. Empty for a plan written before slots existed.
    budget_by_slot: Mapping[str, int] = field(default_factory=dict)
    # Positions with a starting slot we still owe and nothing left to fill it
    # with — either the tier is empty or every survivor is beyond our ceiling.
    # A plan can run out of players as well as money, and only the money was
    # ever measured. See `unfillable`.
    beyond_supply: tuple[Position, ...] = ()

    max_on_one_player: int = 0
    biggest_buy: int = 0

    @property
    def still_calls_for(self) -> int:
        return sum(p.still_calls_for for p in self.positions)

    @property
    def shortfall(self) -> int:
        """Plan dollars still owed, beyond what we have left. 0 is healthy.

        Deliberately conservative in one direction: a position counts as "still
        buying" whenever any slot could take one, bench included. That slightly
        over-states what the plan owes, so the warning arrives early rather than
        late — which is the right way to be wrong about a plan going under.
        """
        return max(0, self.still_calls_for - self.remaining)

    @property
    def slack(self) -> int:
        """Dollars in hand beyond what the plan still calls for."""
        return max(0, self.remaining - self.still_calls_for)

    @property
    def breached_cap(self) -> bool:
        """Did we already pay more for one player than we said we would?"""
        return bool(self.max_on_one_player) and self.biggest_buy > self.max_on_one_player

    @property
    def off_plan(self) -> tuple[PositionAdherence, ...]:
        """Positions materially off, worst overspend first."""
        return tuple(
            sorted(
                (p for p in self.positions if p.is_material),
                key=lambda p: -p.variance,
            )
        )


def _spend_by_position(state: DraftState, book, team_id: int) -> dict[Position, int]:
    """Dollars this team has put into each position.

    A pick whose position never arrived falls back to the reference sheet, and
    if that has nothing either the money is counted in `spent_total` but against
    no position — which is why the per-position figures can sum to less than the
    total, and why they are never used to derive it.
    """
    out: dict[Position, int] = {}
    for player in state.players_for_team(team_id):
        if player.price is None or not player.price.is_known:
            continue

        position = None
        if player.position is not None and player.position.is_known:
            position = player.position.value
        elif book is not None and len(book):
            row = book.get(player.ref.key)
            position = row.position if row else None
        if position is None:
            continue

        out[position] = out.get(position, 0) + player.price.value
    return out


def unfillable(state: DraftState, book, *, scarcity=None, market=None
               ) -> tuple[Position, ...]:
    """Positions with a starting slot we must fill and nothing left to fill it.

    **A plan can fail two ways and only one of them was measured.** `shortfall`
    catches the money running out. This catches the players running out — a
    plan that still earmarks $22 for a flex spot is not on track when every
    startable player left there is beyond our ceiling, however healthy the
    arithmetic looks.

    Two failures, both counted:

    - nothing startable left at all, so the slot cannot be filled by anyone;
    - stock left, but none of it inside our own ceiling. This is
      `Squeeze.priced_out`, asked here without waiting for a squeeze to exist:
      being outbid by the whole room and simply being poor look identical from
      the plan's side, and both mean the same slot goes unfilled.

    Native slots only. A flex opening is answered by whichever position fills it,
    and counting it against three of them would report one hole three times.
    """
    league = state.league
    if league is None or book is None or not len(book):
        return ()

    from ffa.advice.market import inflated_value, market_state
    from ffa.advice.scarcity import scarcity_by_position

    if scarcity is None:
        scarcity = scarcity_by_position(state, book)
    if market is None:
        market = market_state(state, book)

    me = league.my_team_id
    ceiling = proj.max_legal_bid(state, me)
    gaps = proj.starter_gaps(state, me)
    taken = {p.ref.key for p in state.sold_players()}

    out: list[Position] = []
    for position, stock in scarcity.items():
        try:
            slot = RosterSlot(position.value)
        except ValueError:                  # pragma: no cover - every Position maps
            continue
        if gaps.get(slot, 0) <= 0:
            continue                        # nothing to fill here

        supply = stock.elite + stock.startable
        if supply <= 0:
            out.append(position)
            continue

        # Only the startable tier counts. A bench body technically fills the
        # slot and does not fill the *plan*, which is what this is about.
        rows = sorted(
            (r for r in book.by_position(position) if r.key not in taken),
            key=lambda r: -(book.value(r.key) or 0),
        )[:supply]
        affordable = sum(
            1 for r in rows
            if (inflated_value(book, r.key, market) or book.value(r.key) or 0) <= ceiling
        )
        if affordable <= 0:
            out.append(position)

    return tuple(out)


def strategy_read(
    state: DraftState, book, preset: StrategyPreset | None
) -> StrategyRead | None:
    """Adherence to the plan, or `None` when there is no plan to adhere to.

    No plan is a normal state — the tool drafted perfectly well without one — so
    this returns nothing rather than an empty read that a surface would then
    render as a row of zeroes.
    """
    league = state.league
    if league is None or preset is None or not preset.is_set:
        return None

    me = league.my_team_id
    spent_by_position = _spend_by_position(state, book, me)
    open_slots = proj.open_slots_by_pos(state, me)

    # Read through `by_position`, never the stored field: when the plan is
    # declared per slot - which is how `ffa strategy init` writes it now - the
    # positional totals are the sum of those slots and are not in the file at
    # all. FLEX contributes to no position, by design; it has none.
    planned_by_position = preset.by_position

    positions = tuple(
        PositionAdherence(
            position=position,
            planned=planned_by_position.get(position, 0),
            spent=spent_by_position.get(position, 0),
            open_slots=open_slots.get(position, 0),
        )
        # Every position we planned for or spent at. A position with neither is
        # not a silent omission, it is genuinely not part of this draft for us.
        for position in Position
        if planned_by_position.get(position) or spent_by_position.get(position)
    )

    prices = [
        player.price.value
        for player in state.players_for_team(me)
        if player.price is not None and player.price.is_known
    ]

    return StrategyRead(
        archetype=preset.archetype,
        budget=league.budget,
        planned_total=preset.planned_total,
        bench_reserve=preset.bench_reserve,
        spent_total=proj.spent(state, me),
        remaining=proj.remaining_budget(state, me),
        unknown_prices=proj.unknown_price_count(state, me),
        positions=positions,
        budget_by_slot=dict(preset.budget_by_slot),
        beyond_supply=unfillable(state, book),
        max_on_one_player=preset.max_on_one_player,
        biggest_buy=max(prices, default=0),
    )
