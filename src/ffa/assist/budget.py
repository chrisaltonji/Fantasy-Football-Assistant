"""What a draft costs, measured rather than estimated.

Priced per million tokens, from the model table. Cache reads are a tenth of the
input rate and cache writes a quarter above it, which is the whole reason the
prefix is worth building carefully — five thousand tokens of dossiers at full
price on every one of 180 nominations is a different invoice entirely.

The cap is **per draft, not per call**. A per-call target sounds tidy and invites
tuning the Room down while the Grader quietly runs away with the budget; the
number anybody actually cares about is the one at the bottom of the statement.
"""

from __future__ import annotations

from dataclasses import dataclass

# Dollars per million tokens: (input, output).
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_READ = 0.1        # a tenth of the input rate
CACHE_WRITE = 1.25      # a quarter above it, paid once


def cost_cents(usage: dict, model: str) -> int:
    """Cost of one call, in whole cents, rounded up to avoid free-looking calls."""
    rate_in, rate_out = PRICES.get(model, PRICES["claude-opus-5"])
    dollars = (
        int(usage.get("input", 0) or 0) * rate_in
        + int(usage.get("cache_read", 0) or 0) * rate_in * CACHE_READ
        + int(usage.get("cache_creation", 0) or 0) * rate_in * CACHE_WRITE
        + int(usage.get("output", 0) or 0) * rate_out
    ) / 1_000_000
    cents = dollars * 100
    return int(cents) if cents == int(cents) else int(cents) + 1


@dataclass
class SpendGuard:
    """A hard ceiling on one draft. Mutes rather than throttles.

    Throttling would leave you guessing which nominations got a read and which
    were quietly skipped. Stopping outright, once, with a line saying so, is the
    version you can reason about at pick 90.
    """

    cap_dollars: float = 25.0
    spent_cents: int = 0

    def record(self, cents: int) -> None:
        self.spent_cents += max(0, cents)

    @property
    def spent_dollars(self) -> float:
        return self.spent_cents / 100

    @property
    def exceeded(self) -> bool:
        return self.cap_dollars > 0 and self.spent_dollars >= self.cap_dollars

    def reason(self) -> str:
        return (
            f"assistant muted: ${self.spent_dollars:.2f} spent against a "
            f"${self.cap_dollars:.2f} cap. The draft is unaffected — everything "
            "on screen is arithmetic and still exact. Raise it with "
            "--assist-budget."
        )
