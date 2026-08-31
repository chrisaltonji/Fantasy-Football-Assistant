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
    # **Dollars per starting slot, and the declared thing.** Keyed "QB", "RB1",
    # "RB2", ... "FLEX" - see `slot_keys`. This is what you tune, because a plan
    # that says $82 for two running backs does not say whether you meant $50/$32
    # or $41/$41, and those are different drafts.
    budget_by_slot: Mapping[str, int] = field(default_factory=dict)
    # Dollars per position. **Derived from the slots when there are any** - read
    # `by_position`, not this, unless you specifically want what was written in
    # the file. Kept as a field because a plan declared before slots existed has
    # only this, and it must keep working.
    budget_by_position: Mapping[Position, int] = field(default_factory=dict)
    # The most you intend to put on any one player. 0 means you did not say.
    max_on_one_player: int = 0
    # Held back for bench bodies, outside the positional plan.
    bench_reserve: int = 0

    @property
    def is_set(self) -> bool:
        return bool(self.budget_by_slot or self.budget_by_position
                    or self.max_on_one_player)

    @property
    def by_position(self) -> Mapping[Position, int]:
        """The positional plan, **derived from the slots when there are any.**

        Slots are the declared thing and positions are their sum, so the two
        cannot disagree — the same reason nothing computable is stored anywhere
        else in this codebase. A plan written before slots existed has only
        positions, and those are returned untouched.

        FLEX contributes to no position. It has none, which is precisely why the
        plan could not budget for it until slots arrived; folding its money into
        RB would make the running-back line mean two different things.
        """
        if not self.budget_by_slot:
            return self.budget_by_position

        out: dict[Position, int] = {}
        for key, dollars in self.budget_by_slot.items():
            position = position_of_slot(key)
            if position is not None:
                out[position] = out.get(position, 0) + int(dollars)
        return out

    @property
    def planned_total(self) -> int:
        """Every dollar the plan spoken for, FLEX included.

        Summed off the slots rather than the positions when slots exist,
        because the positions do not carry the flex line and would under-report
        the plan by exactly the amount you meant to spend on it.
        """
        if self.budget_by_slot:
            return sum(int(v) for v in self.budget_by_slot.values())
        return sum(self.budget_by_position.values())


# The order a person reads their own roster in, and the order the slots are
# named in. Bench and IR are absent on purpose: this is the starting lineup, and
# an empty bench spot is space rather than a plan.
STARTING_ORDER = ("QB", "RB", "WR", "TE", "FLEX", "K", "DST")


def slot_keys(league) -> tuple[str, ...]:
    """Every starting slot, in roster order, named the way you would say them.

    `RB1`, `RB2` — an ordinal per slot where a position has more than one, and
    the bare position where it has exactly one. There is no `RosterSlot.RB1`;
    multiplicity lives in `league.roster` as a count, so the ordinal is a naming
    convention introduced here and nowhere else.

    **The order is the roster's, not the market's.** These become rows on a
    screen read under a clock, and a lineup that reorders itself as prices move
    is one you have to re-find every time you look at it.
    """
    from ffa.domain.enums import RosterSlot

    roster = getattr(league, "roster", None) or {}
    out: list[str] = []
    for name in STARTING_ORDER:
        try:
            slot = RosterSlot(name)
        except ValueError:                 # a league without that slot at all
            continue
        count = int(roster.get(slot, 0) or 0)
        if count <= 0:
            continue
        if count == 1:
            out.append(name)
        else:
            out.extend(f"{name}{i}" for i in range(1, count + 1))
    return tuple(out)


def position_of_slot(key: str) -> "Position | None":
    """The position a slot key belongs to, or `None` for FLEX.

    `None` is a real answer and the callers depend on it: FLEX has no position,
    which is exactly why the plan could never budget for it until slots existed.
    """
    stem = key.rstrip("0123456789")
    if stem == "FLEX":
        return None
    try:
        return Position.parse(stem)
    except ValueError:
        return None


def slot_split(total: int, count: int, max_share: float) -> list[int]:
    """One position's money across its slots, shaped by the archetype.

    **The archetype is already a concentration preference and nothing else** —
    `max_share` is "the most of the budget you intend to put on any one player".
    So it is the right and only thing to shape this with: stars-and-scrubs wants
    its running-back money in one large piece and a small one, a hoarder wants
    two nearly equal ones. Inventing a second shaping rule when the config
    already carries a concentration number would give two dials for one idea.

    Geometric decay at `1 - max_share`: stars-and-scrubs (0.55) decays to 0.45x
    per slot, a hoarder (0.22) to 0.78x. For a balanced plan and $82 across two
    running backs that is $50 and $32, which is what a real draft looks like —
    an even $41/$41 is a shape no plan actually has.

    Every slot gets at least $1, because a starting slot you have budgeted zero
    for is not a plan, it is an omission. Rounding leftovers go to the first
    slot so the split totals exactly what it was given.
    """
    if count <= 0 or total <= 0:
        return [max(0, total)] if count == 1 else [0] * max(0, count)
    if count == 1:
        return [total]

    decay = max(0.05, min(0.95, 1.0 - float(max_share)))
    weights = [decay ** i for i in range(count)]
    grand = sum(weights)

    out = [max(1, int(total * w / grand)) for w in weights]
    # Never spend more than was allocated, even after the $1 floors.
    while sum(out) > total and max(out) > 1:
        out[out.index(max(out))] -= 1
    leftover = total - sum(out)
    if leftover > 0:
        out[0] += leftover
    return out


def bench_floor(league) -> int:
    """The least a bench reserve can be and still buy a bench.

    Every bench spot costs at least the $1 minimum, so a reserve below this is
    not a lean plan, it is an unfollowable one — you would reach the last round
    with slots to fill and nothing left earmarked for them.

    Derivable from the roster alone, which is why it is a floor rather than a
    preference: it needs no history and no archetype to be true.
    """
    from ffa.domain.enums import RosterSlot

    roster = getattr(league, "roster", None) or {}
    return int(roster.get(RosterSlot.BE, 0))


def opening_max_bid(league) -> int:
    """The most anyone can legally bid on the first player of the draft.

    Every *other* roster slot still has to be bought at the $1 minimum, so that
    money is not available for this player. This is the same arithmetic
    `projections.max_legal_bid` does mid-draft, restated for an empty roster —
    and it is the real ceiling on a one-player cap. Declaring a cap above it is
    not aggressive, it is inert: you could never reach the number.

    Derived from the roster and the budget, like `bench_floor`, so it needs no
    archetype and no history to be true.
    """
    budget = int(getattr(league, "budget", 0) or 0)
    slots = int(getattr(league, "draftable_slots", 0) or 0)
    if budget <= 0 or slots <= 0:
        return budget
    return max(1, budget - (slots - 1))


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


def default_preset(
    league, book, archetype: str = "balanced", *,
    bench_reserve: int | None = None, max_on_one_player: int | None = None,
) -> StrategyPreset:
    """A starting plan built from the market, for you to edit rather than invent.

    A blank `[strategy]` table would be honest and useless — nobody hand-writes
    a twelve-position dollar split from nothing at eleven at night. This gives
    real numbers off the same sheet the bid advice already trusts, so editing it
    is a series of small opinions instead of one large one.

    `bench_reserve` overrides the archetype's share, and every dollar it frees
    goes back into the positional split at market shares. It exists because the
    archetype's share is the one number here with nothing behind it: the split
    is derived from the sheet and the concentration is a stated preference, but
    "10% for the bench" is a guess that a league's own record can flatly
    contradict. This league's four seasons put the median bench at the $1-per-slot
    floor, against an archetype default four times that — so the override is not
    a tuning knob, it is how you replace a guess with a measurement.
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
            max_on_one_player=(
                max_on_one_player if max_on_one_player is not None
                else (int(round(budget * shape.max_share)) if budget else 0)
            ),
            bench_reserve=(
                bench_reserve if bench_reserve is not None
                else (int(round(budget * shape.bench_reserve_share)) if budget else 0)
            ),
        )

    reserve = (
        bench_reserve if bench_reserve is not None
        else int(round(budget * shape.bench_reserve_share))
    )
    reserve = max(reserve, bench_floor(league))
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
        budget_by_slot=seed_slots(league, planned, shape.max_share),
        max_on_one_player=(
            max_on_one_player if max_on_one_player is not None
            else int(round(budget * shape.max_share))
        ),
        bench_reserve=reserve,
    )


def flex_fraction(league, position: Position) -> float:
    """How much of a position's budget was really flex money all along.

    `scarcity.starting_demand` counts a flex-eligible position as its own slots
    *plus an even share of the flex slot* — a 12-team league starting two backs
    and a flex is asking about 2⅓ backs per team. The positional split is built
    off that demand, so RB's dollars have always included a third of a flex
    slot's worth. This is the fraction of them that was.

    Making it explicit is the whole reason FLEX can have a budget now: the money
    is not new, it was folded into three positions where nobody could see or
    tune it.
    """
    from ffa.domain.enums import RosterSlot

    roster = getattr(league, "roster", None) or {}
    eligible = tuple(getattr(league, "flex_positions", ()) or ())
    if position not in eligible:
        return 0.0

    flex_slots = int(roster.get(RosterSlot.FLEX, 0) or 0)
    if flex_slots <= 0:
        return 0.0
    try:
        native = int(roster.get(RosterSlot(position.value), 0) or 0)
    except ValueError:
        return 0.0

    share = flex_slots / len(eligible)
    denominator = native + share
    return (share / denominator) if denominator > 0 else 0.0


def seed_slots(league, planned: Mapping[Position, int],
               max_share: float) -> dict[str, int]:
    """A per-slot plan off the positional one, shaped by the archetype.

    Two steps, and the first is the one worth explaining. The flex money is
    pulled back out of the positions that were implicitly carrying it — see
    `flex_fraction` — so the FLEX line is funded from where its money already
    was rather than invented on top. The plan totals what it totalled before.

    Then each position's remainder is split across its own slots by
    `slot_split`, which shapes it with the archetype's concentration. A position
    with one slot is untouched by both steps.
    """
    keys = slot_keys(league)
    if not keys:
        return {}

    remaining: dict[Position, int] = {}
    flex_pot = 0
    for position, dollars in planned.items():
        share = int(round(dollars * flex_fraction(league, position)))
        flex_pot += share
        remaining[position] = max(0, dollars - share)

    out: dict[str, int] = {}
    for position, dollars in remaining.items():
        native = [k for k in keys if position_of_slot(k) is position]
        if not native:
            # A position the plan budgets but this roster does not start - the
            # money is real, so it is kept rather than quietly dropped.
            out[position.value] = dollars
            continue
        for key, amount in zip(native, slot_split(dollars, len(native), max_share)):
            out[key] = amount

    if "FLEX" in keys:
        out["FLEX"] = flex_pot

    # Emitted in roster order so the file reads like a lineup rather than a
    # dictionary. Anything the loop above invented outside `keys` trails it.
    ordered = {k: out[k] for k in keys if k in out}
    ordered.update({k: v for k, v in out.items() if k not in ordered})
    return ordered


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

    slot_raw = raw.get("budget_by_slot") or {}
    slots: dict[str, int] = {}
    for key, value in slot_raw.items():
        name = str(key).strip().upper()
        # Validated to name a real slot, because a typo is otherwise a silent
        # hole in the plan: `RB3` in a two-back league would parse, total, and
        # never appear against anything.
        if position_of_slot(name) is None and name != "FLEX":
            raise ConfigError(
                f"[strategy.budget_by_slot] has unknown slot {key!r}. Slots are "
                "a position, optionally numbered: QB, RB1, RB2, WR1, TE, FLEX, K"
            )
        try:
            dollars = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"[strategy.budget_by_slot].{key} must be whole dollars, "
                f"got {value!r}"
            ) from exc
        if dollars < 0:
            raise ConfigError(f"[strategy.budget_by_slot].{key} cannot be negative")
        slots[name] = dollars

    if slots and budget:
        # Both would be two sources for one number, and the positional one is
        # the derivable half. Refused rather than reconciled: a plan whose two
        # halves disagree is worse than one that will not load.
        raise ConfigError(
            "[strategy] declares both budget_by_slot and budget_by_position. "
            "Slots are the plan and positions are their sum — keep the slots "
            "and delete budget_by_position."
        )

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
        budget_by_slot=slots,
        budget_by_position=budget,
        max_on_one_player=whole("max_on_one_player"),
        bench_reserve=whole("bench_reserve"),
    )


def strategy_to_dict(preset: StrategyPreset) -> dict[str, Any]:
    """Inverse of `parse_strategy`, for writing `config/league.toml`.

    Writes the slots when there are any and the positions otherwise — never
    both, which `parse_strategy` refuses to read back. Slot order is the
    roster's, as `seed_slots` left it, so the file reads like a lineup.
    """
    out: dict[str, Any] = {
        "archetype": preset.archetype,
        "max_on_one_player": preset.max_on_one_player,
        "bench_reserve": preset.bench_reserve,
    }
    if preset.budget_by_slot:
        out["budget_by_slot"] = dict(preset.budget_by_slot)
    else:
        out["budget_by_position"] = {
            position.value: dollars
            for position, dollars in sorted(
                preset.budget_by_position.items(), key=lambda kv: kv[0].value
            )
        }
    return out


def strategy_problem(
    preset: StrategyPreset, budget: int, league=None
) -> str | None:
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
    if league is not None:
        floor = bench_floor(league)
        if (preset.budget_by_slot or preset.budget_by_position) and preset.bench_reserve < floor:
            return (
                f"the plan holds back ${preset.bench_reserve} for the bench, but "
                f"{floor} bench slot(s) cost at least $1 each — it cannot be filled"
            )

    if preset.max_on_one_player and budget and preset.max_on_one_player > budget:
        return (
            f"max_on_one_player is ${preset.max_on_one_player}, more than the "
            f"whole ${budget} budget"
        )
    if preset.max_on_one_player and league is not None:
        ceiling = opening_max_bid(league)
        if preset.max_on_one_player > ceiling:
            return (
                f"max_on_one_player is ${preset.max_on_one_player}, but the most "
                f"anyone can legally bid on their first player is ${ceiling} — the "
                "other roster slots still cost $1 each, so the cap can never bind"
            )
    return None
