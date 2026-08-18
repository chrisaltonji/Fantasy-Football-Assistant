"""Turning four drafts into a dossier draft you can argue with.

The archetypes the dossier asks about — spend shape, pace, chasing, nomination
style, positional bias, homerism — are exactly what four seasons of real prices
measure. So this emits them in the interchange format `ffa dossier import`
already accepts.

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

# `random` is the residual bucket in the nomination classifier — it means "no
# pattern found", which is not the same as "this person nominates at random" and
# should never be presented as a finding.
NOT_A_FINDING = {"random"}


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
    by_owner = {owner.strip("{}").upper(): team_id for team_id, owner in seats.items()}
    out: dict[str, dict] = {}

    for profile in profiles:
        team_id = by_owner.get(profile.owner_id.strip("{}").upper())
        if team_id is None:
            continue  # no longer in the league
        pooled = profile.pooled
        if pooled is None or len(profile.seasons) < min_seasons:
            continue

        entry: dict = {}
        if profile.manager:
            entry["real_name"] = profile.manager
        entry["seasons_in_league"] = len(profile.seasons)

        for field in ("spend_shape", "pace", "chases"):
            value = getattr(pooled, field)
            if _agreed(profile, field, value):
                entry[field] = value

        style = pooled.nomination_style
        if style not in NOT_A_FINDING and _agreed(profile, "nomination_style", style):
            entry["nomination_style"] = style

        if profile.overpays_at:
            entry["overpays_at"] = list(profile.overpays_at)
        if profile.ignores:
            entry["ignores"] = list(profile.ignores)
        if profile.homer_teams:
            entry["homer_teams"] = list(profile.homer_teams)
        if profile.avoids_teams:
            entry["avoids_teams"] = list(profile.avoids_teams)

        entry["notes"] = _note(profile)
        out[str(team_id)] = entry

    return out


def _note(profile: ManagerProfile) -> str:
    """One line saying where these answers came from, in the record itself.

    Whoever reads this dossier in three seasons should not have to guess whether
    a field was somebody's memory or a script's arithmetic.
    """
    pooled = profile.pooled
    years = ", ".join(str(y) for y in profile.years)
    return (
        f"Derived from ESPN draft history ({years}): "
        f"{len(pooled.picks)} picks, ${pooled.spend:,} spent, "
        f"top-3 buys = {pooled.top3_share * 100:.0f}% of budget, "
        f"{pooled.first_share * 100:.0f}% of money gone in the first third, "
        f"paid {pooled.premium_index:.2f}x reference, "
        f"won {pooled.self_win_rate * 100:.0f}% of own nominations. "
        "Review before trusting."
    )
