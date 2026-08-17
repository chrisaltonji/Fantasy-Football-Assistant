"""Looking players up by whatever the user actually typed.

This is R1 — player identity mismatch is the likeliest source of confidently
wrong advice, because it fails silently: you get a real number attached to the
wrong person. So the rule here is absolute:

    **Never auto-accept a low-confidence match.**

Below the threshold, or when more than one candidate ties, the caller gets
candidates to disambiguate rather than a guess. A wrong match that looks right
is far worse than a prompt.

Matching runs cheapest-and-surest first: exact key, then unique last name (what
people actually type mid-draft — `mahomes`, not `patrick mahomes`), then prefix,
then token subset, and only then fuzzy edit distance for genuine typos.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ffa.domain.enums import Position
from ffa.domain.ids import normalize_player_key
from ffa.reference.loader import LoadReport, ReferenceRow

# A match at or above this is safe to take without asking.
CONFIDENT = 0.85
# Below this a candidate isn't worth showing at all.
FLOOR = 0.55


@dataclass(frozen=True)
class Match:
    row: ReferenceRow
    confidence: float
    how: str

    @property
    def is_confident(self) -> bool:
        return self.confidence >= CONFIDENT


@dataclass(frozen=True)
class Resolution:
    """Either a match we'd stake a bid on, or the candidates to choose from."""

    match: Match | None = None
    candidates: tuple[Match, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.match is not None

    @property
    def is_ambiguous(self) -> bool:
        return self.match is None and bool(self.candidates)


class PlayerBook:
    """Immutable index over one loaded reference file."""

    def __init__(
        self,
        rows: Sequence[ReferenceRow],
        *,
        source: Path | None = None,
        scale: float = 1.0,
        is_sample: bool = False,
        warnings: Sequence[str] = (),
    ) -> None:
        self.rows = tuple(rows)
        self.source = source
        self.scale = scale
        self.is_sample = is_sample
        self.warnings = tuple(warnings)

        self._by_key: dict[str, ReferenceRow] = {r.key: r for r in self.rows}
        self._values = _scaled_values(self.rows, scale)

        self._by_last: dict[str, list[ReferenceRow]] = {}
        for row in self.rows:
            parts = row.key.split("-")
            if parts:
                self._by_last.setdefault(parts[-1], []).append(row)

    # --- construction --------------------------------------------------------

    @classmethod
    def from_report(
        cls,
        report: LoadReport,
        *,
        teams: int = 12,
        budget: int = 200,
        baseline_teams: int = 12,
        baseline_budget: int = 200,
        is_sample: bool = False,
    ) -> "PlayerBook":
        """Build from a load, rescaling values to this league's money supply.

        A safety net rather than the main event: the source calculator already
        rescales to league settings, so exporting at the real settings makes
        this a no-op. It exists for the case where someone exports a stock
        12-team $200 sheet into a differently-shaped league.
        """
        scale = (teams * budget) / max(baseline_teams * baseline_budget, 1)
        return cls(
            report.rows, source=report.source, scale=scale,
            is_sample=is_sample, warnings=report.warnings,
        )

    @classmethod
    def empty(cls) -> "PlayerBook":
        return cls(())

    # --- lookups --------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.rows)

    def get(self, key: str) -> ReferenceRow | None:
        return self._by_key.get(key)

    def value(self, key: str) -> int | None:
        """Reference auction value in whole dollars, rescaled to this league."""
        return self._values.get(key)

    def by_position(self, position: Position) -> tuple[ReferenceRow, ...]:
        return tuple(r for r in self.rows if r.position is position)

    def resolve(self, text: str, *, exclude: Iterable[str] = ()) -> Resolution:
        """Best-effort identification of a typed name."""
        typed = normalize_player_key(text)
        if not typed or not self.rows:
            return Resolution()

        skip = set(exclude)
        exact = self._by_key.get(typed)
        if exact is not None and exact.key not in skip:
            return Resolution(match=Match(exact, 1.0, "exact"))

        scored: dict[str, Match] = {}

        def offer(row: ReferenceRow, confidence: float, how: str) -> None:
            if row.key in skip:
                return
            current = scored.get(row.key)
            if current is None or confidence > current.confidence:
                scored[row.key] = Match(row, confidence, how)

        # Last name — how people actually type under time pressure.
        for row in self._by_last.get(typed, ()):
            offer(row, 0.95, "last name")

        for row in self.rows:
            if row.key == typed:
                continue
            if row.key.startswith(typed + "-") or row.key.startswith(typed):
                offer(row, 0.90, "prefix")

        # Every token typed appears somewhere in the candidate: "ja marr chase".
        tokens = [t for t in typed.split("-") if t]
        if tokens:
            for row in self.rows:
                parts = set(row.key.split("-"))
                if all(any(p.startswith(t) for p in parts) for t in tokens):
                    offer(row, 0.88, "tokens")

        # Genuine typos, against full keys...
        for key in difflib.get_close_matches(typed, self._by_key, n=6, cutoff=FLOOR):
            ratio = difflib.SequenceMatcher(None, typed, key).ratio()
            offer(self._by_key[key], min(ratio, 0.84), "fuzzy")

        # ...and against surnames, which is where typos actually land. Comparing
        # "mahmoes" to the full key "patrick-mahomes" scores too low to survive
        # the cutoff, but against "mahomes" it's an obvious near-miss.
        probes = {typed}
        if "-" in typed:
            probes.add(typed.rsplit("-", 1)[-1])
        for probe in probes:
            for last in difflib.get_close_matches(probe, self._by_last, n=6, cutoff=FLOOR):
                ratio = difflib.SequenceMatcher(None, probe, last).ratio()
                for candidate in self._by_last[last]:
                    # Capped below the confidence threshold on purpose: a near
                    # miss is always worth confirming, never worth assuming.
                    offer(candidate, min(ratio, 0.84), "fuzzy last name")

        ranked = sorted(
            scored.values(), key=lambda m: (-m.confidence, m.row.overall_rank or 9999)
        )
        if not ranked:
            return Resolution()

        best = ranked[0]
        tied = [m for m in ranked if m.confidence >= CONFIDENT]
        # One confident candidate and nothing else contesting it: take it.
        if best.is_confident and len(tied) == 1:
            return Resolution(match=best)

        return Resolution(candidates=tuple(ranked[:5]))

    def suggest(self, text: str, limit: int = 5) -> tuple[Match, ...]:
        resolution = self.resolve(text)
        if resolution.match is not None:
            return (resolution.match,)
        return resolution.candidates[:limit]


def _scaled_values(rows: Sequence[ReferenceRow], scale: float) -> dict[str, int]:
    """Whole-dollar values, deriving from rank where the sheet has no value.

    Rank-derived values are deliberately crude — an exponential decay fitted to
    a typical auction curve. The loader warns when it has to fall back to this,
    because it is much rougher than a real value column.
    """
    priced = [r for r in rows if r.auction_value is not None]
    values: dict[str, int] = {}

    if priced:
        for row in priced:
            values[row.key] = max(1, round(row.auction_value * scale))

    unpriced = [r for r in rows if r.auction_value is None and r.overall_rank]
    if unpriced:
        top = max((values.get(r.key, 0) for r in priced), default=60)
        span = max(len(rows), 1)
        for row in unpriced:
            decayed = top * pow(2.718281828, -4.1 * (row.overall_rank - 1) / span)
            values[row.key] = max(1, round(decayed * scale))

    return values


def load_playerbook(
    path: Path,
    *,
    teams: int = 12,
    budget: int = 200,
    baseline_teams: int = 12,
    baseline_budget: int = 200,
    is_sample: bool = False,
) -> PlayerBook:
    from ffa.reference.loader import load_reference

    return PlayerBook.from_report(
        load_reference(path), teams=teams, budget=budget,
        baseline_teams=baseline_teams, baseline_budget=baseline_budget,
        is_sample=is_sample,
    )
