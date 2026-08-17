"""Degrading sim output on purpose.

The engine's hardest path is not the happy one. It is the partial observation:
ESPN tells us *who* won a player but not *for how much*, and the price arrives
later — or never. That path runs through `Sourced`, `merge`, `remaining_budget`'s
`$1` floor, and the `$142+` rendering, and none of it is exercised by a clean
simulation.

So the simulator can lie the same ways ESPN can:

    ffa sim --drop-prices 0.3      # 30% of sales arrive with no price
    ffa sim --drop-positions 0.1   # 10% arrive with no position
    ffa sim --late-prices 0.5      # half the dropped prices show up later

Faults are drawn from the run's seeded RNG, so a seed reproduces the exact
same degraded draft — which is what makes a failure debuggable rather than a
story about something that happened once.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class FaultProfile:
    """How unreliable this run's observations are.

    All rates are probabilities in [0, 1]. The default is a clean run: the
    simulator should be honest unless asked not to be.
    """

    drop_prices: float = 0.0
    drop_positions: float = 0.0
    # Of the prices that were dropped, the share that arrive later as a
    # follow-up amendment. This is the flagship path: sale first, price later.
    late_prices: float = 1.0

    def __post_init__(self) -> None:
        for name in ("drop_prices", "drop_positions", "late_prices"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")

    @property
    def is_clean(self) -> bool:
        return self.drop_prices == 0.0 and self.drop_positions == 0.0

    def describe(self) -> str:
        if self.is_clean:
            return "clean (no faults injected)"
        parts = []
        if self.drop_prices:
            parts.append(f"{self.drop_prices:.0%} prices dropped")
            parts.append(f"{self.late_prices:.0%} of those arrive late")
        if self.drop_positions:
            parts.append(f"{self.drop_positions:.0%} positions dropped")
        return ", ".join(parts)


class FaultInjector:
    """Decides, per observation, whether the sim gets to see the truth."""

    def __init__(self, profile: FaultProfile, rng: random.Random) -> None:
        self.profile = profile
        self._rng = rng
        self.prices_dropped = 0
        self.prices_recovered = 0
        self.positions_dropped = 0

    def price_is_visible(self) -> bool:
        if self._rng.random() >= self.profile.drop_prices:
            return True
        self.prices_dropped += 1
        return False

    def price_arrives_late(self) -> bool:
        """Called only for a price that was dropped."""
        if self._rng.random() < self.profile.late_prices:
            self.prices_recovered += 1
            return True
        return False

    def position_is_visible(self) -> bool:
        if self._rng.random() >= self.profile.drop_positions:
            return True
        self.positions_dropped += 1
        return False

    def summary(self) -> str:
        if self.profile.is_clean:
            return "no faults injected"
        never = self.prices_dropped - self.prices_recovered
        return (
            f"{self.prices_dropped} price(s) dropped, {self.prices_recovered} "
            f"recovered later, {never} never known; "
            f"{self.positions_dropped} position(s) dropped"
        )
