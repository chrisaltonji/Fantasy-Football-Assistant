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
from typing import Mapping

from ffa.domain.enums import Position


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
