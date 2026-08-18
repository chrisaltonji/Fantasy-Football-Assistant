"""Result shapes for the advisory layer.

Render-agnostic by design: these carry numbers and flags, never formatted text.
The terminal, the dashboard, and the chat layer each present them differently.

Everything here is **deterministic** — computed from ground truth. The soft
half of the advisory capabilities (who is *likely* to bid, tendency reads,
notability judgments) lives in the Claude layer, which consumes these rather
than recomputing them.

One record is marked out. `PaceRead` is also arithmetic and also deterministic,
but its ground truth is a *previous* auction rather than this one. It rides on
`BidGuidance` because `render_guidance` receives nothing else; it never enters
`Threat`, which is tonight's capacity and nothing else; and it never enters the
bid arithmetic — delete every `PaceRead` and `max_advisable_bid` is unchanged.
There is a test that says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping

from ffa.domain.enums import Position

if TYPE_CHECKING:  # pragma: no cover
    from ffa.advice.strategy import StrategyRead


@dataclass(frozen=True)
class PositionScarcity:
    """What's left at one position, in tiers that mean something.

    "Startable" is defined against *this* league's actual starting demand
    rather than an abstract cutoff: if 12 teams each start 2 RBs plus a
    flex, the startable RBs are the top ~30, and the 31st is a bench body
    no matter what a generic tier column says.
    """

    position: Position
    elite: int = 0
    startable: int = 0
    bench: int = 0
    total_remaining: int = 0
    starting_demand: int = 0
    top_value_remaining: int = 0

    @property
    def is_drying_up(self) -> bool:
        """No elite left and startable stock below one per team."""
        return self.elite == 0 and self.startable <= self.starting_demand // 4


@dataclass(frozen=True)
class MarketState:
    """How the room is pricing relative to reference values."""

    sales_priced: int = 0
    dollars_spent: int = 0
    reference_spent: int = 0
    dollars_remaining: int = 0
    inflation_ratio: float | None = None

    @property
    def read(self) -> str:
        """Coarse label. The nuanced read is the Claude layer's job."""
        if self.inflation_ratio is None:
            return "unknown"
        if self.inflation_ratio >= 1.12:
            return "inflated"
        if self.inflation_ratio <= 0.90:
            return "deflated"
        return "neutral"


@dataclass(frozen=True)
class Threat:
    """One rival's *capacity* to take a player. Capacity, not intent.

    Whether they will actually bid is inference. Whether they physically can —
    money on hand, a slot to put him in — is arithmetic, and it belongs here.
    """

    team_id: int
    label: str
    remaining: int
    max_legal_bid: int
    remaining_is_floor: bool
    has_starter_gap: bool
    open_at_position: int

    @property
    def can_bid(self) -> bool:
        return self.max_legal_bid > 0 and self.open_at_position > 0

    @property
    def is_live(self) -> bool:
        """Can afford him *and* has a starting slot for him.

        The distinction the whole tool exists to make: a cash-rich team with no
        room at the position is not a threat there.
        """
        return self.can_bid and self.has_starter_gap


@dataclass(frozen=True)
class PaceRead:
    """One rival measured against their own spending script. Not capacity.

    `Threat` answers what a rival *can* do tonight, to the dollar. This answers
    something the board cannot: whether that number is normal for this person at
    this point in a draft. By pick 54 this league runs from 60% of budget spent
    to 90% — a $60 swing in ammunition that no arithmetic over tonight's picks
    would reveal.

    It is a precedent and not a prediction, which is why `seasons` travels with
    it and `is_thin` is a field rather than a footnote.
    """

    team_id: int
    label: str
    spent: int
    budget: int
    actual_share: float
    expected_share: float
    # How far this manager's own seasons disagree at this point in the board.
    wobble: float = 0.0
    seasons: int = 0
    is_thin: bool = False

    @property
    def dollars_vs_script(self) -> int:
        """Negative means money still in hand that they usually have spent."""
        return round((self.actual_share - self.expected_share) * self.budget)

    @property
    def is_notable(self) -> bool:
        """Worth a line only if it beats both a floor and their own noise.

        The dollar floor stops $4 interrupting a draft. The wobble check is the
        one that matters: a manager whose own four seasons span 40% of budget at
        this point has no script to be off, and reporting one would be inventing
        precision the record does not have.
        """
        departure = abs(self.actual_share - self.expected_share)
        return (
            abs(self.dollars_vs_script) >= 8
            and departure > max(self.wobble, 0.05)
        )


@dataclass(frozen=True)
class BidGuidance:
    """What the numbers say about one nominated player."""

    key: str
    name: str
    position: Position | None = None
    reference_value: int | None = None
    inflated_value: int | None = None
    max_legal_bid: int = 0
    max_advisable_bid: int = 0
    suggested_low: int = 0
    suggested_high: int = 0
    fills_starter_gap: bool = False
    threats: tuple[Threat, ...] = ()
    reasons: tuple[str, ...] = ()

    # Rivals against their own precedent. Deliberately parallel to `threats`
    # rather than a field on one: a `Threat` is tonight's capacity, and mixing a
    # measurement from 2023 into it would blur the line the whole module rests
    # on. Joined by `team_id` at render time.
    pace_reads: tuple[PaceRead, ...] = ()

    # `max_legal_bid` charges our own unpriced picks at the $1 minimum, which
    # *over*-states what we have left. That floor is the safe direction for
    # judging a rival's ammunition — assume they can outbid us — and the
    # dangerous one for setting our own ceiling. The simulator found this
    # directly: with `--drop-prices 0.3`, teams bid against the inflated figure
    # and finished over budget once the real prices landed.
    #
    # So `safe_legal_bid` restates the same arithmetic with those picks charged
    # at what they most likely cost, and it is what caps the advisable bid.
    # `max_legal_bid` is left alone — it is a defined quantity, and quietly
    # redefining it would confuse the two ceilings this codebase is careful to
    # keep apart.
    safe_legal_bid: int = 0
    unknown_prices: int = 0

    # C2's `max_on_one_player`, carried for display and **never** applied.
    # `max_advisable_bid` is what the market says he is worth; folding a declared
    # preference into it would produce a readout saying a player is worth $58
    # because you once said $58. It rides here the way `pace_reads` does — see
    # the test that deletes every strategy field and asserts the arithmetic is
    # unchanged.
    plan_cap: int = 0

    @property
    def exceeds_plan_cap(self) -> bool:
        """Worth more than you said you would spend on anyone."""
        return bool(self.plan_cap) and self.max_advisable_bid > self.plan_cap

    @property
    def ceiling_is_optimistic(self) -> bool:
        """True when our own ceiling is a guess resting on unpriced picks."""
        return self.unknown_prices > 0 and self.safe_legal_bid < self.max_legal_bid

    @property
    def live_threats(self) -> tuple[Threat, ...]:
        return tuple(t for t in self.threats if t.is_live)

    @property
    def contested_ceiling(self) -> int:
        """The highest any *live* rival could legally go."""
        return max((t.max_legal_bid for t in self.live_threats), default=0)


@dataclass(frozen=True)
class NominationTurn:
    """One seat's turn to put a name up, counted in nominations not picks.

    An auction resolves every nomination with a sale, so nomination index and
    pick number advance together and `index` is both.
    """

    index: int          # 0-based, across the whole draft
    round_number: int   # 1-based
    team_id: int
    label: str
    is_me: bool = False


@dataclass(frozen=True)
class NominationCandidate:
    """A player considered as something to *put up*, not something to buy.

    Every field is arithmetic already computed elsewhere for the bid readout;
    this record exists so the same numbers can be read a player at a time across
    the whole remaining board rather than one nominated player at a time.
    """

    key: str
    name: str
    position: Position | None
    value: int                      # inflated where the market says so
    live_rivals: int                # rivals who can afford him *and* start him
    contested_ceiling: int          # the highest any live rival could legally go
    fills_my_starter_gap: bool
    my_ceiling: int                 # our own safe ceiling, for comparison


@dataclass(frozen=True)
class NominationPlan:
    """Whose turn it is, when ours comes, and what the board says to put up.

    The schedule half is knowable before the draft starts and is exact. The
    candidate half is deliberately thin, and the reason is the line this whole
    module is built on: *which* name to put up depends on a strategy — drain the
    room, chase your guys, sit on your money — and a strategy preset is a thing
    this tool does not have yet (C2). So the only lists here are the two that
    need no preference to justify:

    - `bargains` — players who fill a starting hole for us that **no rival can
      both afford and start**. Nominating one is not a strategy, it is free
      money: the auction has nobody to bid it up.
    - `out_of_reach` — players who cost more than our own ceiling and that a live
      rival can actually pay for. Nominating one cannot cost us a player we could
      have had, because we could not have had him. Whether draining a rival is
      *wise* is still inference, and this does not claim it is.

    Everything else a surface might want — will this position survive until my
    turn, who is likely to chase whom — is inference over `scarcity`,
    `nominations_until_mine` and the dossiers, all of which reach the LLM layer
    in the same payload. It is not computed here because it cannot be.
    """

    order_known: bool = False
    turns_taken: int = 0
    total_nominations: int = 0

    # The seat that must put the next name up.
    on_the_clock: NominationTurn | None = None
    upcoming: tuple[NominationTurn, ...] = ()

    # Who the schedule credits with the player currently up for bid. `None` when
    # nothing is nominated. Kept apart from `on_the_clock` because they are
    # different seats the moment a nomination is live, and conflating them is
    # how a readout starts naming the wrong manager.
    current_nominator: NominationTurn | None = None

    my_next: NominationTurn | None = None
    my_turns_left: int = 0

    bargains: tuple[NominationCandidate, ...] = ()
    out_of_reach: tuple[NominationCandidate, ...] = ()
    bargains_total: int = 0
    out_of_reach_total: int = 0

    # Whether the remaining board was actually priced and read. False with no
    # reference file loaded, and the distinction matters: two empty lists then
    # mean "we did not look", not "there is nothing". Saying "nothing is priced
    # past your ceiling" without a single price on hand is a claim, not a
    # summary.
    board_read: bool = False

    # Set when the board disagrees with the schedule about who nominated the
    # player on the block. The schedule is a model of ESPN's order; the board is
    # what happened. When they part company the model is the thing that is
    # wrong, and it says so instead of continuing to assert a seat.
    disagreement: str = ""

    @property
    def nominations_until_mine(self) -> int | None:
        """How many *other* names go up before ours.

        Counted from the next nomination still to be made, not from the last
        completed sale. With a player already on the block those differ by one,
        and the version that counts a nomination already made is the one that
        tells you four when three people are ahead of you.

        0 therefore means we are next: right now if the board is clear, or the
        moment the player on the block sells.
        """
        if self.my_next is None:
            return None
        base = self.turns_taken + (1 if self.current_nominator is not None else 0)
        return max(0, self.my_next.index - base)

    @property
    def is_my_turn(self) -> bool:
        return self.on_the_clock is not None and self.on_the_clock.is_me

    @property
    def has_anything_to_say(self) -> bool:
        """False when there is no order and no candidate — render nothing."""
        return bool(
            self.order_known or self.bargains or self.out_of_reach
        )


@dataclass(frozen=True)
class ValueAlert:
    """A finalized sale that crossed the dynamic overspend threshold."""

    key: str
    name: str
    price: int
    reference_value: int
    inflated_value: int
    delta: int
    over: bool


@dataclass(frozen=True)
class Advisory:
    """Everything the deterministic layer knows right now."""

    market: MarketState = field(default_factory=MarketState)
    scarcity: Mapping[Position, PositionScarcity] = field(default_factory=dict)
    guidance: BidGuidance | None = None
    nomination: NominationPlan | None = None
    # Capability 12. `None` when no plan is declared, which is a normal state.
    strategy: "StrategyRead | None" = None
