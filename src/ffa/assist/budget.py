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

# **2.0, not 1.25, because the breakpoint asks for a one-hour TTL.** The write
# premium is 1.25x at the default five-minute TTL and 2x at an hour;
# `prompts.CACHE_TTL` picks the hour, and this was pricing the other one. It
# under-reported every cache write by 37%, which is the unsafe direction — the
# cap is a safety rail and a rail that reads low is worse than one that reads
# high. Writes are rare, so the absolute error was small; the sign was wrong.
CACHE_WRITE = 2.0


def cost_dollars(usage: dict, model: str) -> float:
    """Exact cost of one call. The only place the price table is applied."""
    rate_in, rate_out = PRICES.get(model, PRICES["claude-opus-5"])
    return (
        int(usage.get("input", 0) or 0) * rate_in
        + int(usage.get("cache_read", 0) or 0) * rate_in * CACHE_READ
        + int(usage.get("cache_creation", 0) or 0) * rate_in * CACHE_WRITE
        + int(usage.get("output", 0) or 0) * rate_out
    ) / 1_000_000


def cost_micros(usage: dict, model: str) -> int:
    """Cost of one call in millionths of a dollar, rounded up.

    **Micros rather than cents, and a live rehearsal is why.** This was whole
    cents rounded up, which is right when the cheapest agent costs 4c: rounding
    0.039 up to 4 is a 2% error and it stops a call ever looking free.

    It stops being right the moment an agent costs less than a cent. The tick
    costs ~0.29c and was billed a full cent — **3.4x over** — and it is also the
    most numerous call by far, so the error compounded into the one number the
    spend cap reads. Measured over a real run: $0.37 spent, $0.51 charged, and
    the assistant muted at roughly two thirds of the budget it was given.

    The round-up survives, at a granularity where it is a rounding error again.
    """
    micros = cost_dollars(usage, model) * 1_000_000
    return int(micros) if micros == int(micros) else int(micros) + 1


def cost_cents(usage: dict, model: str) -> int:
    """Whole cents, rounded up. For display only — never for the cap.

    Kept because a cent is the unit a person reads. Anything that accumulates
    should use `cost_micros`; summing this is how the 3.4x above happened.
    """
    cents = cost_dollars(usage, model) * 100
    return int(cents) if cents == int(cents) else int(cents) + 1


@dataclass
class SpendGuard:
    """A hard ceiling on one draft. Mutes rather than throttles.

    Throttling would leave you guessing which nominations got a read and which
    were quietly skipped. Stopping outright, once, with a line saying so, is the
    version you can reason about at pick 90.
    """

    cap_dollars: float = 25.0
    spent_micros: int = 0

    def record(self, micros: int) -> None:
        self.spent_micros += max(0, micros)

    @property
    def spent_dollars(self) -> float:
        return self.spent_micros / 1_000_000

    @property
    def spent_cents(self) -> int:
        """What was spent, in whole cents. Reading only — see `cost_cents`."""
        return int(self.spent_micros / 10_000)

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
