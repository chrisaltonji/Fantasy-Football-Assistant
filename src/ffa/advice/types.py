"""Result shapes for the advisory layer.

Render-agnostic by design: these carry numbers and flags, never formatted text.
The terminal, the dashboard, and the chat layer each present them differently.

Everything here is **deterministic** — computed from ground truth. The soft
half of the advisory capabilities (who is *likely* to bid, tendency reads,
notability judgments) lives in the Claude layer, which consumes these rather
than recomputing them.
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
