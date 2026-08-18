"""A manager's own spending script, and how far tonight has strayed from it.

This is the one thing four prior auctions can tell you that tonight's board
cannot. Budgets and roster slots are already exact — `max_legal_bid` knows to the
dollar what a rival can spend. What it cannot know is whether $124 is a lot for
*that person* at pick 40, and that turns out to be the widest gap in the data:
by pick 54 this league runs from 60% of budget spent to 90%, a **$60 swing in
ammunition** that no amount of arithmetic over tonight's picks would reveal.

Three things hold this honest.

**It is only computed where it discriminates.** An auction front-loads by
construction, so by the halfway mark every manager has spent about 90% and the
curves collapse into each other. The window is measured from the data rather
than assumed — `discriminating_window` walks the deciles and stops where
between-manager spread stops beating within-manager spread — so the readout goes
quiet on its own instead of confidently reporting noise for two hours.

**It is a precedent, not a prediction.** The record says what somebody did four
times; whether they do it again tonight is a judgement, and the number of
seasons behind every figure travels with it.

**It never touches the bid arithmetic.** `max_legal_bid` and
`max_advisable_bid` are computed from tonight's board and stay that way. This
rides alongside as a separate record, for the same reason the dossier does.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from ffa.history.schema import History

DEFAULT_PRECEDENT_PATH = Path("data/manager_precedent.json")
PRECEDENT_SCHEMA_VERSION = 1

DECILES = 10

# Below this a difference is not worth interrupting a draft for. Somebody being
# $4 off their usual pace is noise; $40 is a different manager.
MIN_DOLLARS = 8

# Two seasons is a pattern of two. Enough to show, flagged as thin.
THIN_SEASONS = 2

# Nothing is said about the first few picks. Before this point the expected
# share and its uncertainty are both near zero, so buying a single player reads
# as being "ahead of script" by whatever he cost — which a dry run duly printed
# as `6% of budget out, usually 0% by now`. Nobody is ahead of anything at pick
# four; there is simply no script yet to be ahead of.
MIN_PROGRESS = 0.05


@dataclass(frozen=True)
class ManagerPrecedent:
    """One manager's spending script, averaged over the seasons on record."""

    owner_id: str
    manager: str = ""
    seasons: tuple[int, ...] = ()
    accounts: tuple[str, ...] = ()
    budget: int = 200

    # Cumulative share of budget spent, at each tenth of the board.
    curve: tuple[float, ...] = ()
    # Half the season-to-season range at each decile: how reliable this is.
    spread: tuple[float, ...] = ()

    spend_shape: str = ""
    pace: str = ""
    nomination_premium: float = 0.0
    te_share: float = 0.0

    @property
    def season_count(self) -> int:
        return len(self.seasons)

    @property
    def is_thin(self) -> bool:
        return self.season_count <= THIN_SEASONS

    def _sample(self, progress: float, series: Sequence[float]) -> float | None:
        """Linear interpolation over a decile series, anchored at the origin.

        `series[i]` is the value at the **end** of decile `i`, so the samples sit
        at 0.1, 0.2 … 1.0 with an implicit zero at progress 0. Treating
        `series[0]` as the value at progress 0 shifts everything a whole decile
        early — which at pick 18 reported a manager as $148 behind a script they
        were exactly on.
        """
        if not series:
            return None
        progress = max(0.0, min(1.0, progress))
        position = progress * DECILES
        upper = min(int(position) if position % 1 else int(position) - 1, DECILES - 1)
        upper = max(upper, 0)
        if position <= 1:
            return series[0] * position
        lower_value = series[upper - 1] if upper >= 1 else 0.0
        upper_value = series[upper]
        weight = position - upper
        return lower_value + (upper_value - lower_value) * weight

    def expected_share(self, progress: float) -> float | None:
        """Share of budget this manager normally has spent by here."""
        return self._sample(progress, self.curve)

    def expected_wobble(self, progress: float) -> float:
        """How much this manager's own seasons disagree at this point.

        The early deciles are volatile even for a consistent person — one year
        they win the first player they want and one year they do not — so a
        departure only means something once it clears their own noise.
        """
        return self._sample(progress, self.spread) or 0.0


@dataclass(frozen=True)
class PrecedentBook:
    """Every manager's script, plus what it is safe to say and when."""

    league_id: int = 0
    seasons: tuple[int, ...] = ()
    window: float = 0.0  # board progress past which the curves stop separating
    managers: Mapping[str, ManagerPrecedent] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    source: Path | None = None

    def for_owner(self, owner_id: str | None) -> ManagerPrecedent | None:
        from ffa.config.identity import fold_owner_id

        if not owner_id:
            return None
        return self.managers.get(fold_owner_id(owner_id))

    def for_team(self, team_id: int, seats: Mapping[int, str]) -> ManagerPrecedent | None:
        return self.for_owner(seats.get(int(team_id)))

    def discriminates_at(self, progress: float) -> bool:
        """Is the board in the range where a comparison means anything?

        Bounded at both ends. Past `window` the curves have converged and every
        manager looks the same; before `MIN_PROGRESS` they have not diverged yet
        and everyone looks extraordinary.
        """
        return bool(self.window) and MIN_PROGRESS <= progress <= self.window

    def __len__(self) -> int:
        return len(self.managers)

    def __bool__(self) -> bool:
        return bool(self.managers)

    @classmethod
    def empty(cls) -> "PrecedentBook":
        return cls()


# --- building it -------------------------------------------------------------------


def _curve(picks: Sequence, board: int, budget: int) -> list[float]:
    return [
        sum(p.price for p in picks if p.overall_pick <= board * (d + 1) / DECILES) / budget
        for d in range(DECILES)
    ]


def discriminating_window(curves: Mapping[str, list[list[float]]]) -> float:
    """How far into the board the managers still differ more than they wobble.

    Measured, never assumed. Every auction front-loads, so the interesting
    question is not "when do people spend" but "when does *who they are* stop
    predicting it" — and past that point a confident readout is just noise
    delivered under pressure.
    """
    multi = [c for c in curves.values() if len(c) >= 2]
    if len(multi) < 2:
        return 0.0

    # Tolerate a shorter series than DECILES: a caller may be probing with a
    # coarse curve, and indexing past the end would be a crash rather than an
    # answer.
    depth = min(len(season) for c in multi for season in c)
    last = 0
    for decile in range(min(DECILES, depth)):
        within = statistics.fmean(
            [statistics.pstdev([season[decile] for season in c]) for c in multi]
        )
        between = statistics.pstdev(
            [statistics.fmean([season[decile] for season in c]) for c in multi]
        )
        if between > within:
            last = decile + 1
        elif last:
            break
    return last / min(DECILES, depth)


def build_precedent(history: History, profiles: Sequence) -> PrecedentBook:
    """Turn analysed profiles into the artifact the draft loads."""
    boards = {
        season.year: max((p.overall_pick for p in season.picks), default=0)
        for season in history.seasons
    }
    budgets = {season.year: season.budget for season in history.seasons}

    curves: dict[str, list[list[float]]] = {}
    for profile in profiles:
        for season in profile.seasons:
            board = boards.get(season.year, 0)
            if not board:
                continue
            curves.setdefault(profile.owner_id, []).append(
                _curve(season.picks, board, budgets.get(season.year, 200))
            )

    window = discriminating_window(curves)
    managers: dict[str, ManagerPrecedent] = {}

    for profile in profiles:
        seasons = curves.get(profile.owner_id) or []
        if not seasons:
            continue
        mean = [statistics.fmean([s[d] for s in seasons]) for d in range(DECILES)]
        spread = [
            (max(s[d] for s in seasons) - min(s[d] for s in seasons)) / 2
            for d in range(DECILES)
        ]
        pooled = profile.pooled
        te = next((s for s in pooled.positions if s.position == "TE"), None)
        managers[profile.owner_id] = ManagerPrecedent(
            owner_id=profile.owner_id,
            manager=profile.manager,
            seasons=tuple(s.year for s in profile.seasons),
            accounts=tuple(profile.accounts),
            budget=budgets.get(profile.seasons[-1].year, 200) if profile.seasons else 200,
            curve=tuple(round(v, 4) for v in mean),
            spread=tuple(round(v, 4) for v in spread),
            spend_shape=pooled.spend_shape,
            pace=pooled.pace,
            nomination_premium=round(pooled.nomination_premium, 2),
            te_share=round(te.share, 4) if te else 0.0,
        )

    return PrecedentBook(
        league_id=history.league_id,
        seasons=tuple(history.years),
        window=window,
        managers=managers,
    )


# --- disk ---------------------------------------------------------------------------


def save_precedent(book: PrecedentBook, path: Path = DEFAULT_PRECEDENT_PATH) -> None:
    payload = {
        "schema_version": PRECEDENT_SCHEMA_VERSION,
        "league_id": book.league_id,
        "seasons": list(book.seasons),
        "window": round(book.window, 3),
        "managers": {
            owner: {
                "manager": p.manager,
                "seasons": list(p.seasons),
                "accounts": list(p.accounts),
                "budget": p.budget,
                "curve": list(p.curve),
                "spread": list(p.spread),
                "spend_shape": p.spend_shape,
                "pace": p.pace,
                "nomination_premium": p.nomination_premium,
                "te_share": p.te_share,
            }
            for owner, p in sorted(book.managers.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_precedent(
    path: Path = DEFAULT_PRECEDENT_PATH, *, league_id: int = 0
) -> PrecedentBook:
    """Read the artifact. A missing or broken one costs the readout, never the draft.

    Same contract as `load_dossiers`: everything else works without this, and
    refusing to start a draft over a derived file would be absurd.
    """
    if not path.is_file():
        return PrecedentBook(source=path)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return PrecedentBook(source=path, warnings=(f"could not read {path}: {exc}",))

    if not isinstance(data, dict):
        return PrecedentBook(source=path, warnings=(f"{path} is not an object",))

    warnings: list[str] = []
    found = int(data.get("league_id") or 0)
    if league_id and found and found != league_id:
        # A precedent file from another league would describe strangers with
        # total confidence.
        return PrecedentBook(
            source=path,
            warnings=(
                f"{path} was built for league {found}, not {league_id} — ignoring it",
            ),
        )

    managers: dict[str, ManagerPrecedent] = {}
    for owner, raw in (data.get("managers") or {}).items():
        if not isinstance(raw, dict):
            warnings.append(f"{path}: entry {owner!r} is not an object — skipped")
            continue
        curve = [float(v) for v in (raw.get("curve") or [])]
        if len(curve) != DECILES:
            warnings.append(f"{path}: {owner} has no usable curve — skipped")
            continue
        managers[owner] = ManagerPrecedent(
            owner_id=owner,
            manager=str(raw.get("manager") or ""),
            seasons=tuple(int(y) for y in (raw.get("seasons") or [])),
            accounts=tuple(str(a) for a in (raw.get("accounts") or [])),
            budget=int(raw.get("budget") or 200),
            curve=tuple(curve),
            spread=tuple(float(v) for v in (raw.get("spread") or [])),
            spend_shape=str(raw.get("spend_shape") or ""),
            pace=str(raw.get("pace") or ""),
            nomination_premium=float(raw.get("nomination_premium") or 0.0),
            te_share=float(raw.get("te_share") or 0.0),
        )

    return PrecedentBook(
        league_id=found,
        seasons=tuple(int(y) for y in (data.get("seasons") or [])),
        window=float(data.get("window") or 0.0),
        managers=managers,
        warnings=tuple(warnings),
        source=path,
    )
