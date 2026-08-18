"""C2 — the draft plan you declared before the draft started.

Everything else in this codebase computes. This does not: it records what *you
said you would do*, so that capability 12 can measure what you actually did
against it. That makes it the second thing in here that is testimony rather
than arithmetic, after the owner dossiers — and it carries the same risk, which
is that a number sitting beside computed ones gets read as computed. So the
vocabulary stays deliberately blunt: `planned`, never `projected`; `variance`,
never `error`.

**Two decisions worth knowing before changing any of it.**

*The plan is not captured in the journal.* `LeagueSnapshot` copies league
settings into `DraftInitialized` precisely so that editing `league.toml`
mid-draft cannot move a budget ceiling underneath you. A plan is the opposite
case: it is an intention, revising it mid-draft is a legitimate thing to do, and
adherence should measure against the plan you hold *now*. Nothing here feeds a
ceiling, a threat, or a legal bid, so letting it stay live costs no correctness.

*The default allocation is derived, not invented.* An archetype could just as
easily have shipped a hardcoded "RB 35%, WR 35%" table, and that table would be
somebody's opinion wearing the costume of a default. Instead the split comes
from the reference sheet itself — what this league's starters at each position
actually cost, at the money this league actually has. The archetype only sets
*concentration*: how much you will put on one player, and how much you hold back
for the bench. Those are genuinely preferences, and there is nothing to derive
them from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ffa.domain.enums import Position


@dataclass(frozen=True)
class Archetype:
    """A concentration preference, and nothing more.

    Deliberately *not* a positional split. "Stars and scrubs" is not a claim
    that running backs deserve more of the budget — it is a claim about spending
    it in fewer, larger pieces, which is what `max_share` says. The positional
    split it would otherwise imply is derivable from the market, so deriving it
    is strictly better than asserting it.

    Names match `sim/bots.py` on purpose. The bots already model a room of these
    people; using a different vocabulary for the same idea would leave you
    translating between two glossaries under a clock.
    """

    name: str
    # The most of the budget you intend to put on any one player.
    max_share: float
    # Held back for bench bodies, and not part of the positional plan.
    bench_reserve_share: float
    note: str = ""


ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        "stars-and-scrubs", max_share=0.55, bench_reserve_share=0.06,
        note="a few big buys, minimum-price everywhere else",
    ),
    Archetype(
        "balanced", max_share=0.35, bench_reserve_share=0.10,
        note="the market's own shape, no position starved",
    ),
    Archetype(
        "value-hunter", max_share=0.28, bench_reserve_share=0.12,
        note="never top of the market; more, cheaper pieces",
    ),
    Archetype(
        "hoarder", max_share=0.22, bench_reserve_share=0.15,
        note="money late, when the room has none",
    ),
)

BY_NAME: Mapping[str, Archetype] = {a.name: a for a in ARCHETYPES}


@dataclass(frozen=True)
class StrategyPreset:
    """One declared plan. Empty means no plan, which every reader must respect.

    An absent plan is a normal state, not a broken one — you can draft the whole
    thing without declaring anything, exactly as this tool did until now. Every
    surface therefore has to treat "no plan" as "say nothing", the same way the
    nomination readout treats a missing draft order.
    """

    archetype: str = ""
    # Dollars you intend to spend at each position, over the whole draft.
    budget_by_position: Mapping[Position, int] = field(default_factory=dict)
    # The most you intend to put on any one player. 0 means you did not say.
    max_on_one_player: int = 0
    # Held back for bench bodies, outside the positional plan.
    bench_reserve: int = 0

    @property
    def is_set(self) -> bool:
        return bool(self.budget_by_position or self.max_on_one_player)

    @property
    def planned_total(self) -> int:
        return sum(self.budget_by_position.values())


def market_shape(league, book) -> dict[Position, float]:
    """What share of starter money each position commands, per the sheet.

    Reads the top *N* values at each position where N is this league's total
    starting demand there — the same definition `advice/scarcity.py` tiers
    against, so a position is measured by what this league actually starts
    rather than by an abstract cutoff. A 12-team league starting two RBs and a
    flex is asking about the top ~30 RBs; a 10-team league is not.

    Returns shares that sum to 1.0, or `{}` when there is nothing to read.
    """
    from ffa.advice.scarcity import starting_demand

    if league is None or book is None or not len(book):
        return {}

    totals: dict[Position, float] = {}
    for position in Position:
        demand = starting_demand(league, position)
        if demand <= 0:
            continue
        values = sorted(
            (book.value(row.key) or 0 for row in book.by_position(position)),
            reverse=True,
        )[:demand]
        total = float(sum(values))
        if total > 0:
            totals[position] = total

    grand = sum(totals.values())
    if grand <= 0:
        return {}
    return {position: total / grand for position, total in totals.items()}


def default_preset(league, book, archetype: str = "balanced") -> StrategyPreset:
    """A starting plan built from the market, for you to edit rather than invent.

    A blank `[strategy]` table would be honest and useless — nobody hand-writes
    a twelve-position dollar split from nothing at eleven at night. This gives
    real numbers off the same sheet the bid advice already trusts, so editing it
    is a series of small opinions instead of one large one.
    """
    shape = BY_NAME.get(archetype)
    if shape is None:
        raise ValueError(
            f"unknown archetype {archetype!r}. Known: "
            + ", ".join(a.name for a in ARCHETYPES)
        )

    budget = getattr(league, "budget", 0) or 0
    shares = market_shape(league, book)
    if not shares or budget <= 0:
        # Nothing to derive from. Return the concentration half rather than
        # inventing a split — half a plan you can see is better than a whole one
        # you cannot check.
        return StrategyPreset(
            archetype=archetype,
            max_on_one_player=int(round(budget * shape.max_share)) if budget else 0,
            bench_reserve=int(round(budget * shape.bench_reserve_share)) if budget else 0,
        )

    reserve = int(round(budget * shape.bench_reserve_share))
    spendable = max(0, budget - reserve)

    planned = {
        position: max(1, int(share * spendable))
        for position, share in shares.items()
    }

    # Rounding down everywhere leaves dollars unassigned; put them where the
    # market says they matter most, so the plan totals exactly what it says.
    leftover = spendable - sum(planned.values())
    if leftover and planned:
        richest = max(shares, key=lambda p: shares[p])
        planned[richest] = max(1, planned[richest] + leftover)

    return StrategyPreset(
        archetype=archetype,
        budget_by_position=planned,
        max_on_one_player=int(round(budget * shape.max_share)),
        bench_reserve=reserve,
    )


# --- config serialization ---------------------------------------------------


def parse_strategy(raw: Mapping[str, Any] | None) -> StrategyPreset:
    """`[strategy]` -> a preset. A malformed entry raises rather than degrades.

    Silently dropping a bad position would leave you drafting against a plan
    missing a position you thought you had declared, and the readout would look
    perfectly healthy while doing it.
    """
    from ffa.config.schema import ConfigError

    if not raw:
        return StrategyPreset()

    archetype = str(raw.get("archetype", "") or "").strip()
    if archetype and archetype not in BY_NAME:
        raise ConfigError(
            f"[strategy].archetype is {archetype!r}. Known archetypes: "
            + ", ".join(a.name for a in ARCHETYPES)
        )

    budget_raw = raw.get("budget_by_position") or {}
    budget: dict[Position, int] = {}
    for key, value in budget_raw.items():
        try:
            position = Position.parse(str(key))
        except ValueError as exc:
            raise ConfigError(
                f"[strategy.budget_by_position] has unknown position {key!r}. "
                "Valid: " + ", ".join(p.value for p in Position)
            ) from exc
        try:
            dollars = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"[strategy.budget_by_position].{key} must be whole dollars, "
                f"got {value!r}"
            ) from exc
        if dollars < 0:
            raise ConfigError(
                f"[strategy.budget_by_position].{key} cannot be negative"
            )
        budget[position] = dollars

    def whole(name: str) -> int:
        try:
            out = int(raw.get(name, 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"[strategy].{name} must be whole dollars") from exc
        if out < 0:
            raise ConfigError(f"[strategy].{name} cannot be negative")
        return out

    return StrategyPreset(
        archetype=archetype,
        budget_by_position=budget,
        max_on_one_player=whole("max_on_one_player"),
        bench_reserve=whole("bench_reserve"),
    )


def strategy_to_dict(preset: StrategyPreset) -> dict[str, Any]:
    """Inverse of `parse_strategy`, for writing `config/league.toml`."""
    return {
        "archetype": preset.archetype,
        "max_on_one_player": preset.max_on_one_player,
        "bench_reserve": preset.bench_reserve,
        "budget_by_position": {
            position.value: dollars
            for position, dollars in sorted(
                preset.budget_by_position.items(), key=lambda kv: kv[0].value
            )
        },
    }


def strategy_problem(preset: StrategyPreset, budget: int) -> str | None:
    """Why this plan cannot be followed, or `None` if it can.

    Checked at load rather than mid-draft. A plan that asks for more money than
    the league gives you is not a strategy you are failing to keep — it is one
    you were never able to keep, and finding that out at pick 40 is finding it
    out too late.
    """
    if not preset.is_set:
        return None

    committed = preset.planned_total + preset.bench_reserve
    if budget and committed > budget:
        return (
            f"the plan commits ${committed} "
            f"(${preset.planned_total} across positions plus ${preset.bench_reserve} "
            f"held back) but the budget is ${budget}"
        )
    if preset.max_on_one_player and budget and preset.max_on_one_player > budget:
        return (
            f"max_on_one_player is ${preset.max_on_one_player}, more than the "
            f"whole ${budget} budget"
        )
    return None
