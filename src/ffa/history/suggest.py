"""Turning four drafts into a dossier draft you can argue with.

Some of the archetypes the dossier asks about are exactly what four seasons of
real prices measure, and some are not. **Only the ones that cleared a null test
are emitted** — see `ffa.history.evidence`:

| field | z | emitted |
|---|---|---|
| `pace` | +3.4 | yes |
| `skill`, from finishing position | +2.9 | yes |
| `spend_shape` | +2.9 | yes |
| TE bias, via `overpays_at` / `ignores` | +3.0 | yes |
| `chases` | −1.1 | **no** |
| `homer_teams` / `avoids_teams` | −0.3 | **no** |
| RB / WR / QB bias | +0.2 … +1.1 | **no** |
| `nomination_style` | see below | **no** |

`nomination_style` is the interesting exclusion. The *premium* a manager
nominates at — how far above the going rate the players they put up cost — is
the strongest signal in the whole record at z = +5.2. But the dossier's question
is about **intent**: enforcer, targeter, best-available. Telling those apart
needs the self-win rate, which does not survive (z = +0.6), so the data can say
what somebody nominates and not why. The number goes in the report; the question
stays for a human who was in the room.

Positional bias is filtered rather than dropped: TE allocation survives at +3.0
while RB, WR and QB do not, so only TE reaches the file.

Provenance goes in `derived_from`, never in `notes`. `notes` and `tells` are
the two questions no measurement can touch — the things only somebody who has
drafted in the room knows — and they are the reason to run the interview at
all. Filling them with generated text would take the most valuable fields in
the file out of play.

**It is never written straight into the record.** Not because measurement is
untrustworthy, but because the dossier is a store of *observations*, and there
is a real difference between "I watched him do this for six years" and "an
arithmetic mean over four drafts said so". The output goes to a file you read,
disagree with, and import on purpose — which is also the fastest possible way
to run the interview, because arguing with a wrong answer is quicker than
producing a right one from a blank page.

Only fields with real support are emitted. A label the seasons disagreed about,
or one computed off too few priced picks, is left out — an absent key means "not
asked", and inventing an answer here would poison the one thing the file is for.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ffa.history.metrics import ManagerProfile

# A label needs to have held in more than half the seasons on record before it
# is worth putting in front of somebody as a suggestion.
MIN_AGREEMENT = 0.5

# The labels that cleared a null test in `ffa.history.evidence`. Everything
# else is computed and shown in the report as a number, but never handed to the
# dossier — a suggestion there is one `ffa dossier import` away from becoming a
# recorded observation, and a false observation is worse than a missing one.
SURVIVING_LABELS: tuple[str, ...] = ("spend_shape", "pace")

# TE allocation survives at z = +3.0; RB, WR and QB sit between +0.2 and +1.1.
SURVIVING_POSITIONS: frozenset[str] = frozenset({"TE"})


def _agreed(profile: ManagerProfile, field: str, label: str) -> bool:
    """Did this label actually hold across the record, or is it a coin flip?"""
    if not label:
        return False
    summary = profile.consistency.get(field, "")
    if "/" not in summary:
        return False
    hits, _, rest = summary.partition("/")
    total = rest.split(" ")[0]
    try:
        return (int(hits) / int(total)) > MIN_AGREEMENT
    except (ValueError, ZeroDivisionError):
        return False


def suggest_dossiers(
    profiles: Sequence[ManagerProfile],
    seats: Mapping[int, str],
    *,
    min_seasons: int = 2,
) -> dict[str, dict]:
    """`{team_id: {field: value}}`, keyed the way the importer expects."""
    from ffa.config.identity import fold_owner_id

    by_owner = {fold_owner_id(owner): team_id for team_id, owner in seats.items()}
    out: dict[str, dict] = {}

    for profile in profiles:
        team_id = by_owner.get(fold_owner_id(profile.owner_id))
        if team_id is None:
            continue  # no longer in the league
        pooled = profile.pooled
        if pooled is None or len(profile.seasons) < min_seasons:
            continue

        entry: dict = {}
        if profile.manager:
            entry["real_name"] = profile.manager
        entry["seasons_in_league"] = len(profile.seasons)

        for field in SURVIVING_LABELS:
            value = getattr(pooled, field)
            if _agreed(profile, field, value):
                entry[field] = value

        # From the league table rather than the draft board. It clears its own
        # null at z = +2.9 — people land in the same part of this table year
        # after year — and it is the one archetype the record answers better
        # than anybody's memory.
        standing = getattr(profile, "standing", None)
        if standing is not None and getattr(standing, "skill", ""):
            entry["skill"] = standing.skill

        # TE only. The other positions do not clear the null, and a suggestion
        # is one `ffa dossier import` away from becoming an observation.
        overpays = [p for p in profile.overpays_at if p in SURVIVING_POSITIONS]
        ignores = [p for p in profile.ignores if p in SURVIVING_POSITIONS]
        if overpays:
            entry["overpays_at"] = overpays
        if ignores:
            entry["ignores"] = ignores

        # Not `notes`. That field is the human catch-all and is worth more than
        # everything this module can compute; filling it with a generated string
        # would take the most valuable question in the set out of play.
        entry["derived_from"] = _note(profile)
        out[str(team_id)] = entry

    return out


def _note(profile: ManagerProfile) -> str:
    """One line saying where these answers came from, in the record itself.

    Whoever reads this dossier in three seasons should not have to guess whether
    a field was somebody's memory or a script's arithmetic.
    """
    pooled = profile.pooled
    years = ", ".join(str(y) for y in profile.years)
    accounts = (
        f" across {len(profile.accounts)} ESPN accounts"
        if len(getattr(profile, "accounts", ())) > 1
        else ""
    )
    standing = getattr(profile, "standing", None)
    table = f" League table: {standing.summary()}." if standing is not None else ""
    return (
        f"Derived from ESPN draft history ({years}{accounts}): "
        f"{len(pooled.picks)} picks, ${pooled.spend:,} spent, "
        f"top-3 buys = {pooled.top3_share * 100:.0f}% of budget, "
        f"{pooled.first_share * 100:.0f}% of money gone in the first third, "
        f"nominations ran {pooled.nomination_premium:+.1f} vs the going rate."
        f"{table} "
        "Only signals that cleared a permutation test are filled in above, and "
        "`skill` is inferred from finishing position rather than from how "
        "somebody drafts. Nothing here can see a tell; review before trusting."
    )
