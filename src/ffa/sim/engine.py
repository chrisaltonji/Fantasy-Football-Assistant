"""Runs a whole auction and emits the events one would have produced.

The engine is a *generator of events*, not a driver of the store. It yields
`PlayerNominated`, `PlayerSold` and `FieldAmended` and hands them back; whoever
is consuming decides what to do with them. That is what keeps the simulator on
the far side of the `EventSource` seam, with no `if simulating:` anywhere in
the reducers, projections, or store.

Clearing price is second-price-plus-one, the way an English auction actually
ends: bidding stops when the second-most-willing bidder drops out, so the
winner pays what it took to beat them, not their own private ceiling. Pricing
at the winner's ceiling instead would inflate every sale and make the market
model read the room as permanently hot.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator

from ffa.domain import projections as proj
from ffa.domain.enums import Provenance
from ffa.domain.events import (
    BaseEvent,
    DraftInitialized,
    FieldAmended,
    PlayerNominated,
    PlayerSold,
)
from ffa.domain.ids import PlayerRef, format_entity_address
from ffa.domain.models import DraftState
from ffa.domain.reducers import apply
from ffa.domain.sourced import known, unknown
from ffa.reference.playerbook import PlayerBook
from ffa.sim.bots import Bot, build_room
from ffa.sim.faults import FaultInjector, FaultProfile

MIN_BID = 1

# Wall-clock spacing between simulated observations. Real auctions run roughly
# this fast, and giving events distinct timestamps matters: `merge` breaks ties
# on `observed_at`, so a sim where everything happens at T would never exercise
# the newer-wins rule.
SECONDS_PER_PICK = 20


@dataclass
class SimResult:
    """What a completed run produced."""

    events: tuple[BaseEvent, ...] = ()
    sold: int = 0
    unsold: tuple[str, ...] = ()
    faults: str = ""
    seed: int = 0

    @property
    def total_spent(self) -> int:
        return sum(
            e.price.value for e in self.events
            if isinstance(e, PlayerSold) and e.price.is_known
        )


@dataclass
class AuctionSim:
    """A seeded auction over a fixed player pool.

    Determinism is not a nicety. A sim that cannot be replayed exactly turns
    every failure into an anecdote, so everything random draws from `seed`.
    """

    league: DraftInitialized
    book: PlayerBook
    seed: int = 0
    faults: FaultProfile = field(default_factory=FaultProfile)
    human_team: int | None = None
    start_at: datetime | None = None
    pool_size: int | None = None

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._injector = FaultInjector(self.faults, random.Random(self.seed ^ 0x5EED))
        team_ids = [t.team_id for t in self.league.teams]
        self._bots: dict[int, Bot] = build_room(team_ids, self._rng, exclude=self.human_team)
        self._clock = self.start_at or datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)
        self._tick = 0

    # --- the run --------------------------------------------------------------

    def run(self) -> SimResult:
        events = list(self.events())
        sold = [e for e in events if isinstance(e, PlayerSold)]
        return SimResult(
            events=tuple(events),
            sold=len(sold),
            unsold=self._unsold,
            faults=self._injector.summary(),
            seed=self.seed,
        )

    def events(self) -> Iterator[BaseEvent]:
        """Yield the whole draft, one event at a time.

        State is folded forward locally with the real `apply`, so the bots see
        exactly the projections the live tool would compute. Using a shadow
        model here would let the sim pass while the real engine failed.
        """
        state = apply(DraftState.empty(self.league.draft_id), self.league)
        pool = self._initial_pool()
        pending_prices: list[tuple[str, int, datetime]] = []
        order = self._nomination_order()
        self._unsold = ()

        while pool and self._someone_can_still_buy(state):
            nominator = next(order)

            # Late prices land between picks, which is what makes them
            # interesting: the budget is wrong until they arrive.
            for event in self._due_prices(pending_prices):
                state = apply(state, event)
                yield event

            bot = self._bots.get(nominator)
            key = (bot or self._any_bot()).nominate(state, self.book, pool) if self._bots else None
            if key is None:
                break
            pool.remove(key)

            nomination = self._nominate(key, nominator)
            state = apply(state, nomination)
            yield nomination

            sale = self._auction(state, key)
            if sale is None:
                continue  # nobody could bid; he goes unsold

            winner, price = sale
            event, deferred = self._sell(key, winner, price)
            state = apply(state, event)
            yield event
            if deferred is not None:
                pending_prices.append(deferred)

        # Anything still owed gets delivered before the draft closes.
        for event in self._due_prices(pending_prices, flush=True):
            state = apply(state, event)
            yield event

        self._unsold = tuple(sorted(pool))

    # --- auction mechanics ----------------------------------------------------

    def _auction(self, state: DraftState, key: str) -> tuple[int, int] | None:
        """Return (winning_team, clearing_price), or None if nobody bids."""
        bids = [
            (bot.willingness(state, self.book, key), team_id)
            for team_id, bot in self._bots.items()
        ]
        bids = [(amount, team_id) for amount, team_id in bids if amount >= MIN_BID]
        if not bids:
            return None

        # Sort by amount desc, then team id asc — never rely on dict order.
        bids.sort(key=lambda pair: (-pair[0], pair[1]))
        top_amount, winner = bids[0]
        runner_up = bids[1][0] if len(bids) > 1 else 0

        price = min(top_amount, max(MIN_BID, runner_up + 1))
        legal = proj.max_legal_bid(state, winner)
        return winner, max(MIN_BID, min(price, legal))

    def _someone_can_still_buy(self, state: DraftState) -> bool:
        return any(
            proj.max_legal_bid(state, team_id) >= MIN_BID
            for team_id in self._bots
        )

    # --- event construction ---------------------------------------------------

    def _nominate(self, key: str, team_id: int) -> PlayerNominated:
        at = self._advance()
        row = self.book.get(key)
        return PlayerNominated(
            at=at,
            source=Provenance.SIM.value,
            player=PlayerRef.from_raw(row.name if row else key),
            nominated_by=known(team_id, Provenance.SIM, at),
        )

    def _sell(
        self, key: str, team_id: int, price: int
    ) -> tuple[PlayerSold, tuple[str, int, datetime] | None]:
        """Build the sale, dropping what this run's fault profile hides."""
        at = self._advance()
        row = self.book.get(key)

        if self._injector.price_is_visible():
            price_field = known(price, Provenance.SIM, at)
            deferred = None
        else:
            price_field = unknown(at, note="sim: price not observed")
            deferred = (
                (key, price, at + timedelta(seconds=SECONDS_PER_PICK * 3))
                if self._injector.price_arrives_late()
                else None
            )

        position = None
        if row is not None and row.position is not None and self._injector.position_is_visible():
            position = known(row.position, Provenance.SIM, at)

        return (
            PlayerSold(
                at=at,
                source=Provenance.SIM.value,
                player=PlayerRef.from_raw(row.name if row else key),
                team=known(team_id, Provenance.SIM, at),
                price=price_field,
                position=position,
            ),
            deferred,
        )

    def _due_prices(
        self, pending: list[tuple[str, int, datetime]], *, flush: bool = False
    ) -> Iterator[FieldAmended]:
        """Emit late-arriving prices whose time has come.

        This is the flagship provenance path: a sale recorded with an unknown
        price, corrected later by a real observation. `merge`'s "knowledge
        beats ignorance" rule is what makes it land.
        """
        still_pending = []
        for key, price, due in pending:
            if not flush and due > self._clock:
                still_pending.append((key, price, due))
                continue
            at = self._advance()
            # Address by the canonical key, not the display name. The
            # reducer uses this ident as a dict key verbatim, with no
            # normalization, so a name here would silently amend a
            # different entity than the sale created.
            yield FieldAmended(
                at=at,
                source=Provenance.SIM.value,
                entity=format_entity_address("player", key),
                field_name="price",
                value=known(price, Provenance.SIM, at),
            )
        pending[:] = still_pending

    # --- helpers --------------------------------------------------------------

    def _initial_pool(self) -> list[str]:
        rows = sorted(
            self.book.rows,
            key=lambda r: (-(r.auction_value or 0), r.key),
        )
        keys = [r.key for r in rows]
        if not keys:
            return []
        if self.pool_size is not None:
            return keys[: self.pool_size]
        # The whole board, deliberately. Truncating to "enough bodies" sorts by
        # value and therefore cuts kickers and defenses first — they are the
        # cheapest rows in any auction-value export. Teams then cannot fill
        # their K and DST slots, strand the money, and the run looks like a
        # bidding bug when it is really a pool bug.
        return keys

    def _nomination_order(self) -> Iterator[int]:
        """Round-robin, which is what ESPN's `pickOrder` produces."""
        ids = sorted(self._bots)
        if not ids:
            return iter(())

        def cycle() -> Iterator[int]:
            while True:
                for team_id in ids:
                    yield team_id

        return cycle()

    def _any_bot(self) -> Bot:
        return self._bots[min(self._bots)]

    def _advance(self) -> datetime:
        self._tick += 1
        self._clock = self._clock + timedelta(seconds=SECONDS_PER_PICK)
        return self._clock
