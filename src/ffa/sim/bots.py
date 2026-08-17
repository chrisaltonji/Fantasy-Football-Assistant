"""Budget-aware bidders.

Bots read the same projections the advisory layer reads — `max_legal_bid`,
`open_slots_by_pos`, `starter_gaps`. That is the point of the simulator: if
those functions are wrong, the sim produces a draft that is visibly illegal
(a team buying a 16th player, or bidding money it does not have) instead of
the error hiding until draft day.

A bot never touches the store. It answers two questions and nothing else:

- given the pool, who do I nominate?
- given a player, what is the most I would pay?

The auction mechanics live in `engine.py`. Keeping willingness separate from
clearing price is what lets the engine run a second-price auction without the
bots knowing that is what is happening.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ffa.domain import projections as proj
from ffa.domain.enums import Position
from ffa.domain.models import DraftState
from ffa.reference.playerbook import PlayerBook

# A player who only fits on our bench is worth less than one who fills a
# starting hole. Mirrors `advice.bidding.BENCH_DISCOUNT` deliberately: the bots
# should price roughly the way the tool advises, so a sim draft lands in a
# realistic price range rather than a degenerate one.
BENCH_DISCOUNT = 0.55

# Nominating is not the same as wanting. Real managers throw out players they
# have no intention of buying to drain rivals' budgets early.
BAIT_RATE = 0.25


@dataclass(frozen=True)
class Archetype:
    """A bidding personality.

    Diversity here is what makes the sim worth running. A room of identical
    bots clears every player at the same predictable multiple and exercises
    none of the interesting paths — bidding wars, early overspend, teams that
    reach the endgame with money and no slots.
    """

    name: str
    # Multiplier on reference value. >1 overpays, <1 hunts value.
    aggression: float = 1.0
    # How much of the budget this bot is willing to sink into one player.
    max_share: float = 0.40
    # Willingness to spend early vs. hold for the endgame. 1.0 = flat.
    early_bias: float = 1.0
    # Random jitter applied per player, as a fraction of value.
    noise: float = 0.10


ARCHETYPES: tuple[Archetype, ...] = (
    Archetype("stars-and-scrubs", aggression=1.20, max_share=0.55, early_bias=1.25, noise=0.12),
    Archetype("balanced", aggression=1.00, max_share=0.35, early_bias=1.00, noise=0.10),
    Archetype("value-hunter", aggression=0.85, max_share=0.28, early_bias=0.85, noise=0.08),
    Archetype("hoarder", aggression=0.75, max_share=0.22, early_bias=0.70, noise=0.06),
    Archetype("homer", aggression=1.10, max_share=0.45, early_bias=1.10, noise=0.22),
)


def archetype_for(index: int) -> Archetype:
    """Deterministic assignment, so a seed fully describes the room."""
    return ARCHETYPES[index % len(ARCHETYPES)]


@dataclass
class Bot:
    """One simulated manager."""

    team_id: int
    archetype: Archetype
    rng: random.Random

    def willingness(self, state: DraftState, book: PlayerBook, key: str) -> int:
        """The most this bot would pay for this player, right now.

        Always capped by `max_legal_bid`, which is the arithmetic ceiling every
        real team is also under. A bot that returns more than that would let
        the sim produce a draft the real engine considers impossible.
        """
        legal = proj.max_legal_bid(state, self.team_id)
        if legal <= 0:
            return 0

        row = book.get(key)
        reference = book.value(key)
        if reference is None or reference <= 0:
            # No value to reason about. Bots bid the minimum, which is what
            # actually happens to unranked players late in a real auction.
            return min(1, legal)

        position = row.position if row else None
        if position is not None and self._openings(state, position) <= 0:
            return 0  # no slot to put him in, at any price

        want = reference * self.archetype.aggression * self._phase_multiplier(state)
        if position is not None and not self._needs_starter(state, position):
            want *= BENCH_DISCOUNT

        want *= self._surplus(state)

        jitter = 1.0 + self.rng.uniform(-self.archetype.noise, self.archetype.noise)
        want *= jitter

        budget = state.league.budget if state.league else 0
        pace = self._pace(state)
        # The per-player share cap governs the early draft, where discipline is
        # the whole personality. It must not bind in the endgame: a team with
        # $94 and one slot left has to be willing to spend $94, because an
        # unspent dollar is a wasted dollar. The 2025 auction bears this out —
        # every team finished within $5 of zero.
        cap = max(int(budget * self.archetype.max_share), int(pace * 2))
        return max(0, min(int(round(want)), cap, legal))

    def nominate(self, state: DraftState, book: PlayerBook, pool: list[str]) -> str | None:
        """Pick someone to put up for auction.

        Sometimes that is a player this bot wants; sometimes it is bait — an
        expensive player it cannot afford, thrown out to drain the room. Both
        happen constantly in real auctions and they stress different paths.
        """
        if not pool:
            return None

        if self.rng.random() < BAIT_RATE:
            # Bait: the most expensive thing on the board, wanted or not.
            return max(pool, key=lambda k: (book.value(k) or 0, k))

        wanted = [k for k in pool if self.willingness(state, book, k) > 1]
        if not wanted:
            return max(pool, key=lambda k: (book.value(k) or 0, k))

        # Weight toward the top of what it wants, without always taking #1 —
        # otherwise every seeded run nominates in pure value order.
        wanted.sort(key=lambda k: (-(book.value(k) or 0), k))
        cutoff = max(1, len(wanted) // 4)
        return wanted[self.rng.randrange(cutoff)]

    # --- internals ------------------------------------------------------------

    def _openings(self, state: DraftState, position: Position) -> int:
        return proj.open_slots_by_pos(state, self.team_id).get(position, 0)

    def _needs_starter(self, state: DraftState, position: Position) -> bool:
        league = state.league
        if league is None:
            return False
        gaps = proj.starter_gaps(state, self.team_id)
        from ffa.domain.enums import RosterSlot

        return any(
            gaps.get(slot, 0) > 0
            for slot in league.slots_for_position(position)
            if slot is not RosterSlot.BE
        )

    def _pace(self, state: DraftState) -> float:
        """Dollars available per roster spot still to fill."""
        openings = proj.open_draftable_slots(state, self.team_id)
        if openings <= 0:
            return 0.0
        return proj.remaining_budget(state, self.team_id) / openings

    def _surplus(self, state: DraftState) -> float:
        """Bid up when flush relative to what is left to buy.

        A team holding more money per open slot than it started with is behind
        on spending, and real managers respond by paying over the odds rather
        than carrying cash into the endgame. Clamped to never *deflate* a bid:
        a team that overspent early should get poorer, not more timid, which
        `max_legal_bid` already enforces on its own.
        """
        league = state.league
        if league is None or league.draftable_slots == 0:
            return 1.0
        baseline = league.budget / league.draftable_slots
        if baseline <= 0:
            return 1.0
        return min(max(self._pace(state) / baseline, 1.0), 3.0)

    def _phase_multiplier(self, state: DraftState) -> float:
        """Early-draft eagerness, tapering as the roster fills.

        `early_bias` of 1.0 is flat. Above 1.0 the bot front-loads and risks
        arriving at the endgame with $1 slots to fill — which is exactly the
        situation `max_legal_bid` exists to handle, so the sim should produce
        it on purpose.
        """
        league = state.league
        if league is None or league.draftable_slots == 0:
            return 1.0
        filled = proj.roster_count(state, self.team_id)
        progress = filled / league.draftable_slots
        bias = self.archetype.early_bias
        return bias + (1.0 - bias) * progress


def build_room(team_ids: list[int], rng: random.Random, *, exclude: int | None = None) -> dict[int, Bot]:
    """One bot per team, archetypes spread deterministically.

    `exclude` leaves a seat empty for a human — the sim can drive the whole
    room, or every seat but yours so you can practise against it.
    """
    room: dict[int, Bot] = {}
    for index, team_id in enumerate(sorted(team_ids)):
        if team_id == exclude:
            continue
        # A per-bot Random keeps one bot's draws from shifting another's,
        # so changing the number of bots doesn't reshuffle everyone.
        seed = rng.randrange(2**32)
        room[team_id] = Bot(team_id, archetype_for(index), random.Random(seed))
    return room
