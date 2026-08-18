"""Turning four drafts into a read on twelve people.

Every classification here is **league-relative**, and that is the central
decision. An absolute rule — "stars-and-scrubs means top-3 over 45% of budget" —
encodes what auctions look like in general, when the only question that matters
is what they look like *in this room*. A league where everybody front-loads has
no front-loaders in the useful sense; the manager worth flagging is the one who
does it more than the people he is bidding against. So the raw numbers are
computed per manager, then classified by z-score against that season's field.

Two consequences worth keeping in mind:

- **Somebody is always at the edge of the distribution.** A label is a
  statement about rank, not about virtue, and the report prints the underlying
  number beside every one of them so a thin margin is visible as a thin margin.
- **Small samples stay small.** Twelve managers over four seasons is enough to
  rank and not enough to be certain, which is why the aggregate reports how many
  seasons agreed rather than averaging the labels into a single confident word.

Pure: no I/O, no network. Everything takes `SeasonDraft` records and returns
records.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ffa.history.schema import DraftPick, History, SeasonDraft

# A purchase counts as chasing when it clears the reference by both a margin
# *and* an amount. The ratio alone flags a $3 player bought for $5, which is
# noise; the dollar floor alone flags every expensive player in an inflated
# room.
CHASE_RATIO = 1.25
CHASE_DOLLARS = 4

# How far from the league mean a manager has to sit before a label is worth
# printing. 0.6σ is deliberately loose — with twelve managers, insisting on 1σ
# leaves almost everybody unclassified, and "no strong read" is already
# available as an outcome.
LABEL_Z = 0.6

# An affinity needs a rate, a body count, *and* a surplus over what chance
# would hand you anyway. Two Bengals in a 15-round draft is a coincidence; the
# rate alone flags anybody who happened to take three players from a team the
# rest of the room ignored.
AFFINITY_MIN_PICKS = 3
AFFINITY_INDEX = 1.8
AFFINITY_SURPLUS = 2.0

# The most affinities worth printing for one manager. Past about five the list
# stops being a read on a person and starts being a description of noise.
AFFINITY_LIMIT = 5

# ESPN's proTeamId 0. A free agent is not a team anybody is a homer about.
NOT_A_TEAM = {"", "FA"}

# Avoiding a team only means something where the league drafts enough of them
# that staying away takes effort. This is a deliberately weak test and is
# reported as such: with 32 NFL teams and ~59 picks per manager over four
# seasons, the most-drafted team in the league only has an expectation of ~2.9,
# so "avoids" can never be more than suggestive. The dossier asks the question
# directly for exactly this reason.
AVOID_MIN_EXPECTED = 2.5
AVOID_RATIO = 0.34


# --- small statistics ------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _stage(pick_number: int, low: int, high: int) -> int:
    return 0 if pick_number <= low else (1 if pick_number <= high else 2)


def _stage_medians(draft: "SeasonDraft") -> list[float]:
    """The going rate in each third of the draft.

    A $40 nomination means something completely different at pick 8 and at pick
    150, so a nomination is only ever compared against what the room was paying
    at that moment.
    """
    low, high = draft.thirds()
    buckets: list[list[int]] = [[], [], []]
    for pick in draft.picks:
        buckets[_stage(pick.overall_pick, low, high)].append(pick.price)
    return [statistics.median(b) if b else 0.0 for b in buckets]


def _z(value: float, population: Sequence[float]) -> float:
    """How many standard deviations from the field. 0.0 when the field is flat."""
    if len(population) < 2:
        return 0.0
    spread = statistics.pstdev(population)
    if spread <= 1e-9:
        return 0.0
    return (value - statistics.fmean(population)) / spread


def _gini(values: Sequence[float]) -> float:
    """0 = every pick cost the same, 1 = one pick took everything."""
    numbers = sorted(v for v in values if v >= 0)
    n = len(numbers)
    total = sum(numbers)
    if n < 2 or total <= 0:
        return 0.0
    weighted = sum((i + 1) * v for i, v in enumerate(numbers))
    return (2 * weighted) / (n * total) - (n + 1) / n


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Rank correlation. Used to ask whether nominations track player price."""
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0

    def ranked(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            shared = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = shared
            i = j + 1
        return ranks

    rx, ry = ranked(xs), ranked(ys)
    try:
        return statistics.correlation(rx, ry)
    except statistics.StatisticsError:
        return 0.0


# --- records ---------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSplit:
    position: str
    picks: int = 0
    spend: int = 0
    share: float = 0.0            # share of this manager's money
    allocation_index: float = 1.0  # vs the league's share at this position
    avg_price: float = 0.0
    price_index: float = 1.0       # vs the league's average price at this position


@dataclass(frozen=True)
class TeamAffinity:
    pro_team: str
    picks: int = 0
    spend: int = 0
    expected: float = 0.0     # picks the league's own rate would have handed them
    index: float = 1.0        # their share of picks vs the league's
    money_index: float = 1.0  # same, weighted by dollars


@dataclass(frozen=True)
class ManagerSeason:
    """One manager, one draft."""

    owner_id: str
    year: int
    team_id: int = 0
    manager: str = ""
    team_name: str = ""
    picks: tuple[DraftPick, ...] = ()
    budget: int = 200

    # spend shape
    spend: int = 0
    top2_share: float = 0.0
    top3_share: float = 0.0
    gini: float = 0.0
    max_price: int = 0
    cheap_picks: int = 0

    # pace
    first_share: float = 0.0
    middle_share: float = 0.0
    last_share: float = 0.0

    # chasing
    priced_with_reference: int = 0
    overpays: int = 0
    chase_rate: float = 0.0
    premium_index: float = 1.0
    biggest_reaches: tuple[DraftPick, ...] = ()
    biggest_bargains: tuple[DraftPick, ...] = ()

    positions: tuple[PositionSplit, ...] = ()
    affinities: tuple[TeamAffinity, ...] = ()

    # nominations
    nominations: int = 0
    self_wins: int = 0
    self_win_rate: float = 0.0
    nomination_avg_price: float = 0.0
    nomination_premium: float = 0.0   # vs the going rate at that stage
    best_available_index: float = 0.0
    early_nomination_avg_price: float = 0.0

    # labels, assigned once the whole field is known
    spend_shape: str = ""
    pace: str = ""
    chases: str = ""
    nomination_style: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagerProfile:
    """One manager across every season we could read."""

    owner_id: str
    manager: str = ""
    # Every ESPN account folded into this person. More than one means an alias
    # was declared, and the report should say so rather than quietly presenting
    # a merged record as a single account's.
    accounts: tuple[str, ...] = ()
    seasons: tuple[ManagerSeason, ...] = ()
    pooled: ManagerSeason | None = None
    overpays_at: tuple[str, ...] = ()
    ignores: tuple[str, ...] = ()
    homer_teams: tuple[str, ...] = ()
    avoids_teams: tuple[str, ...] = ()
    consistency: Mapping[str, str] = field(default_factory=dict)

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(s.year for s in self.seasons)


# --- one season -------------------------------------------------------------------


def _raw_season(
    draft: SeasonDraft, owner_id: str, team_id: int, managers: Mapping[str, str]
) -> ManagerSeason:
    mine = tuple(p for p in draft.picks if p.team_id == team_id)
    spend = sum(p.price for p in mine)
    prices = sorted((p.price for p in mine), reverse=True)
    budget = draft.budget

    low, high = draft.thirds()
    thirds = [0, 0, 0]
    for pick in mine:
        bucket = 0 if pick.overall_pick <= low else (1 if pick.overall_pick <= high else 2)
        thirds[bucket] += pick.price

    referenced = [p for p in mine if p.reference is not None]
    overpays = [
        p
        for p in referenced
        if p.price >= p.reference * CHASE_RATIO and p.price - p.reference >= CHASE_DOLLARS
    ]
    ref_total = sum(p.reference for p in referenced)
    paid_total = sum(p.price for p in referenced)

    nominated = tuple(p for p in draft.picks if p.nominating_team_id == team_id)
    self_wins = [p for p in nominated if p.team_id == team_id]

    # Do their nominations come out in price order? Correlate the sequence they
    # nominated in against each player's price rank across the whole draft:
    # +1 means strictly best-first.
    #
    # Read this only against the field. Every auction runs roughly in price
    # order because the expensive players go early *by construction*, so every
    # manager here scores about +0.9 and the raw number discriminates nothing.
    # What separates them is who tracks the board more tightly than the room
    # does, which is a z-score, not a threshold.
    price_rank = {
        p.player_id: rank
        for rank, p in enumerate(sorted(draft.picks, key=lambda x: -x.price))
    }
    ordered = sorted(nominated, key=lambda p: p.overall_pick)
    bpa = _spearman(
        list(range(len(ordered))), [price_rank.get(p.player_id, 0) for p in ordered]
    )

    # What their nominations cost relative to what the room was paying at that
    # point in the draft. Positive means they put up players above the going
    # rate — which, paired with a low self-win rate, is what draining somebody
    # else's budget looks like from the outside.
    thirds_median = _stage_medians(draft)
    premium = _mean([
        p.price - thirds_median[_stage(p.overall_pick, low, high)] for p in ordered
    ])

    early = [p for p in ordered if p.overall_pick <= low]

    return ManagerSeason(
        owner_id=owner_id,
        year=draft.year,
        team_id=team_id,
        manager=managers.get(owner_id, ""),
        team_name=draft.team_names.get(team_id, ""),
        picks=mine,
        budget=budget,
        spend=spend,
        top2_share=(sum(prices[:2]) / budget) if budget else 0.0,
        top3_share=(sum(prices[:3]) / budget) if budget else 0.0,
        gini=_gini(prices),
        max_price=prices[0] if prices else 0,
        cheap_picks=sum(1 for v in prices if v <= 2),
        first_share=(thirds[0] / spend) if spend else 0.0,
        middle_share=(thirds[1] / spend) if spend else 0.0,
        last_share=(thirds[2] / spend) if spend else 0.0,
        priced_with_reference=len(referenced),
        overpays=len(overpays),
        chase_rate=(len(overpays) / len(referenced)) if referenced else 0.0,
        premium_index=(paid_total / ref_total) if ref_total > 0 else 1.0,
        biggest_reaches=tuple(sorted(overpays, key=lambda p: -(p.delta or 0))[:5]),
        biggest_bargains=tuple(
            sorted(
                (p for p in referenced if (p.delta or 0) < 0),
                key=lambda p: (p.delta or 0),
            )[:5]
        ),
        nominations=len(nominated),
        self_wins=len(self_wins),
        self_win_rate=(len(self_wins) / len(nominated)) if nominated else 0.0,
        nomination_avg_price=_mean([p.price for p in nominated]),
        nomination_premium=premium,
        best_available_index=bpa,
        early_nomination_avg_price=_mean([p.price for p in early]),
    )


def _position_splits(
    mine: Sequence[DraftPick], league: Sequence[DraftPick]
) -> tuple[PositionSplit, ...]:
    my_spend = sum(p.price for p in mine) or 1
    league_spend = sum(p.price for p in league) or 1
    positions = sorted({p.position for p in league if p.position})

    out = []
    for position in positions:
        theirs = [p for p in mine if p.position == position]
        theirs_spend = sum(p.price for p in theirs)
        all_at = [p for p in league if p.position == position]
        league_share = sum(p.price for p in all_at) / league_spend
        league_avg = _mean([p.price for p in all_at])
        my_avg = _mean([p.price for p in theirs])
        share = theirs_spend / my_spend
        out.append(
            PositionSplit(
                position=position,
                picks=len(theirs),
                spend=theirs_spend,
                share=share,
                allocation_index=(share / league_share) if league_share else 1.0,
                avg_price=my_avg,
                price_index=(my_avg / league_avg) if league_avg else 1.0,
            )
        )
    return tuple(out)


def _affinities(
    mine: Sequence[DraftPick], league: Sequence[DraftPick]
) -> tuple[TeamAffinity, ...]:
    """Which NFL teams a manager takes more of than the room does.

    Measured against **league-wide popularity that season**, not against an even
    split. Half the room having Eagles is a good Eagles year, not twelve
    homers — only somebody above that baseline is showing a bias.
    """
    my_total = len(mine) or 1
    league_total = len(league) or 1
    my_money = sum(p.price for p in mine) or 1
    league_money = sum(p.price for p in league) or 1

    out = []
    for team in sorted({p.pro_team for p in mine if p.pro_team not in NOT_A_TEAM}):
        theirs = [p for p in mine if p.pro_team == team]
        all_at = [p for p in league if p.pro_team == team]
        league_share = len(all_at) / league_total
        league_money_share = sum(p.price for p in all_at) / league_money
        out.append(
            TeamAffinity(
                pro_team=team,
                picks=len(theirs),
                spend=sum(p.price for p in theirs),
                expected=league_share * my_total,
                index=((len(theirs) / my_total) / league_share) if league_share else 1.0,
                money_index=(
                    (sum(p.price for p in theirs) / my_money) / league_money_share
                    if league_money_share
                    else 1.0
                ),
            )
        )
    return tuple(sorted(out, key=lambda a: (-a.index, -a.picks)))


def _homers(affinities: Sequence[TeamAffinity]) -> tuple[str, ...]:
    """Teams this manager takes more of than chance and the room account for."""
    flagged = [
        a
        for a in affinities
        if a.picks >= AFFINITY_MIN_PICKS
        and a.index >= AFFINITY_INDEX
        and a.picks - a.expected >= AFFINITY_SURPLUS
    ]
    ranked = sorted(flagged, key=lambda a: -(a.picks - a.expected))
    return tuple(a.pro_team for a in ranked[:AFFINITY_LIMIT])


def _avoided(
    mine: Sequence[DraftPick], league: Sequence[DraftPick]
) -> tuple[str, ...]:
    """Teams the room drafts heavily that this manager stays away from.

    Under-representation rather than absence. Across four seasons a manager
    makes sixty picks, and demanding a total shutout would report nothing but
    the teams nobody drafts anyway — the mirror image of the homer test has to
    be a rate, exactly as that one is.
    """
    my_total = len(mine) or 1
    league_total = len(league) or 1
    counts: dict[str, int] = {}
    for pick in mine:
        counts[pick.pro_team] = counts.get(pick.pro_team, 0) + 1

    out = []
    for team in sorted({p.pro_team for p in league if p.pro_team not in NOT_A_TEAM}):
        expected = (sum(1 for p in league if p.pro_team == team) / league_total) * my_total
        actual = counts.get(team, 0)
        if expected >= AVOID_MIN_EXPECTED and actual <= expected * AVOID_RATIO:
            out.append((team, expected - actual))
    return tuple(t for t, _ in sorted(out, key=lambda kv: -kv[1])[:AFFINITY_LIMIT])


def _label(value: float, population: Sequence[float], high: str, low: str, mid: str) -> str:
    z = _z(value, population)
    if z >= LABEL_Z:
        return high
    if z <= -LABEL_Z:
        return low
    return mid


def _classify(seasons: Sequence[ManagerSeason], *, has_nominations: bool) -> list[ManagerSeason]:
    """Assign labels once the whole field for that season is known."""
    from dataclasses import replace

    top3 = [s.top3_share for s in seasons]
    first = [s.first_share for s in seasons]
    last = [s.last_share for s in seasons]
    premium = [s.premium_index for s in seasons]
    # `self_win_rate` and `best_available_index` are still computed and still
    # reported as numbers; they simply no longer decide a label, because neither
    # clears its null (z = +0.6 and, once z-scored, indistinguishable too).
    nom_premium = [s.nomination_premium for s in seasons]

    out = []
    for season in seasons:
        notes: list[str] = []

        shape = _label(
            season.top3_share, top3, "stars_and_scrubs", "value_hunter", "balanced"
        )

        # Front-loading and waiting are the same axis read from both ends, so
        # take whichever end this manager sits further from.
        z_first, z_last = _z(season.first_share, first), _z(season.last_share, last)
        if max(z_first, z_last) < LABEL_Z:
            pace = "steady"
        elif z_first >= z_last:
            pace = "front_loads"
        else:
            pace = "waits"

        chases = _label(season.premium_index, premium, "often", "rarely", "sometimes")
        if season.priced_with_reference < 6:
            chases = ""
            notes.append(
                f"too few priced picks with a reference ({season.priced_with_reference}) "
                "to judge chasing"
            )

        # What somebody nominates is measurable; *why* is not. The premium a
        # manager nominates at is the strongest signal in the record (z = +5.2),
        # but telling an enforcer from a targeter needs the self-win rate, which
        # does not survive its null (z = +0.6). So this reports the observable
        # and stops — the intent question stays in the dossier for a human who
        # was in the room.
        style = ""
        if has_nominations and season.nominations >= 5:
            z_premium = _z(season.nomination_premium, nom_premium)
            if z_premium >= LABEL_Z:
                style = "nominates_high"
            elif z_premium <= -LABEL_Z:
                style = "nominates_low"
            else:
                style = "no_pattern"
        elif not has_nominations:
            notes.append("ESPN recorded no nomination order for this season")

        out.append(
            replace(
                season,
                spend_shape=shape,
                pace=pace,
                chases=chases,
                nomination_style=style,
                notes=tuple(notes),
            )
        )
    return out


def analyse_season(
    draft: SeasonDraft, managers: Mapping[str, str] | None = None
) -> list[ManagerSeason]:
    """Every manager in one draft, classified against that draft's field."""
    from dataclasses import replace

    managers = managers or {}
    seats = [(owner, team_id) for team_id, owner in sorted(draft.owners.items())]
    raw = [_raw_season(draft, owner, team_id, managers) for owner, team_id in seats]

    enriched = [
        replace(
            season,
            positions=_position_splits(season.picks, draft.picks),
            affinities=_affinities(season.picks, draft.picks),
        )
        for season in raw
    ]
    return _classify(enriched, has_nominations=draft.has_nominations)


# --- across seasons ----------------------------------------------------------------


def _mode(values: Sequence[str]) -> str:
    """The label a manager wore most often; ties go to the most recent season.

    `values` arrives in season order. A two-two split is a real outcome — people
    change how they draft — and recency is the only tiebreak that means
    anything, so the newer read wins rather than the alphabet.
    """
    real = [v for v in values if v]
    if not real:
        return ""
    counts: dict[str, int] = {}
    for value in real:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts.values())
    winners = [k for k, v in counts.items() if v == best]
    if len(winners) == 1:
        return winners[0]
    for value in reversed(real):
        if value in winners:
            return value
    return winners[0]


def build_profiles(history: History, managers: Mapping[str, str] | None = None):
    """One profile per manager, plus the per-season detail behind it."""
    managers = managers or {}
    by_owner: dict[str, list[ManagerSeason]] = {}
    all_picks: list[DraftPick] = []
    my_picks: dict[str, list[DraftPick]] = {}
    accounts: dict[str, set[str]] = {}

    for draft in history.seasons:
        for season in analyse_season(draft, managers):
            by_owner.setdefault(season.owner_id, []).append(season)
            my_picks.setdefault(season.owner_id, []).extend(season.picks)
            accounts.setdefault(season.owner_id, set()).add(season.owner_id)
        all_picks.extend(draft.picks)

    profiles: list[ManagerProfile] = []
    for owner_id, seasons in by_owner.items():
        seasons = sorted(seasons, key=lambda s: s.year)
        pooled_picks = my_picks.get(owner_id, [])
        positions = _position_splits(pooled_picks, all_picks)
        affinities = _affinities(pooled_picks, all_picks)

        # Pooled shape metrics, so the aggregate is computed on the whole record
        # rather than by averaging four already-rounded labels.
        pooled = ManagerSeason(
            owner_id=owner_id,
            year=0,
            manager=managers.get(owner_id, ""),
            picks=tuple(pooled_picks),
            budget=sum(s.budget for s in seasons),
            spend=sum(s.spend for s in seasons),
            top3_share=_mean([s.top3_share for s in seasons]),
            gini=_gini([p.price for p in pooled_picks]),
            max_price=max((p.price for p in pooled_picks), default=0),
            cheap_picks=sum(s.cheap_picks for s in seasons),
            first_share=_mean([s.first_share for s in seasons]),
            middle_share=_mean([s.middle_share for s in seasons]),
            last_share=_mean([s.last_share for s in seasons]),
            priced_with_reference=sum(s.priced_with_reference for s in seasons),
            overpays=sum(s.overpays for s in seasons),
            chase_rate=_mean([s.chase_rate for s in seasons if s.priced_with_reference]),
            premium_index=_mean([s.premium_index for s in seasons if s.priced_with_reference]),
            positions=positions,
            affinities=affinities,
            nominations=sum(s.nominations for s in seasons),
            self_wins=sum(s.self_wins for s in seasons),
            self_win_rate=_mean([s.self_win_rate for s in seasons if s.nominations]),
            nomination_avg_price=_mean(
                [s.nomination_avg_price for s in seasons if s.nominations]
            ),
            nomination_premium=_mean(
                [s.nomination_premium for s in seasons if s.nominations]
            ),
            best_available_index=_mean(
                [s.best_available_index for s in seasons if s.nominations]
            ),
            spend_shape=_mode([s.spend_shape for s in seasons]),
            pace=_mode([s.pace for s in seasons]),
            chases=_mode([s.chases for s in seasons]),
            nomination_style=_mode([s.nomination_style for s in seasons]),
            biggest_reaches=tuple(
                sorted(
                    (p for s in seasons for p in s.biggest_reaches),
                    key=lambda p: -(p.delta or 0),
                )[:6]
            ),
        )

        profiles.append(
            ManagerProfile(
                owner_id=owner_id,
                manager=managers.get(owner_id, ""),
                accounts=tuple(sorted(accounts.get(owner_id, {owner_id}))),
                seasons=tuple(seasons),
                pooled=pooled,
                overpays_at=tuple(
                    s.position
                    for s in positions
                    if s.allocation_index >= 1.25 and s.spend >= 40
                ),
                ignores=tuple(
                    s.position for s in positions if s.allocation_index <= 0.65
                ),
                homer_teams=_homers(affinities),
                avoids_teams=_avoided(pooled_picks, all_picks),
                consistency={
                    "spend_shape": _agreement([s.spend_shape for s in seasons]),
                    "pace": _agreement([s.pace for s in seasons]),
                    "chases": _agreement([s.chases for s in seasons]),
                    "nomination_style": _agreement([s.nomination_style for s in seasons]),
                },
            )
        )

    return sorted(profiles, key=lambda p: p.manager or p.owner_id)


def _agreement(values: Sequence[str]) -> str:
    """`3/4 stars_and_scrubs` — how much of the record actually backs a label."""
    real = [v for v in values if v]
    if not real:
        return "no reading"
    counts: dict[str, int] = {}
    for value in real:
        counts[value] = counts.get(value, 0) + 1
    label, hits = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return f"{hits}/{len(real)} {label}"
