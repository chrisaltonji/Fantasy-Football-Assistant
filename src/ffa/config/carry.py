"""What survives `ffa config init --force`.

Regenerating the config is the documented fix for a lost or stale
``league.toml``, so it must not quietly undo the settings ESPN knows nothing
about. That list used to be six field names hardcoded into a `dataclasses.replace`
call, which meant **every new config field was silently wiped by the recovery
command** until somebody noticed — and the way you notice is that a manager you
aliased splits back into two people halfway through a report.

So the list lives here, next to a test that walks every field of `LeagueConfig`
and fails if a field is in neither this tuple nor the from-ESPN set. Adding a
field now forces a decision about it rather than defaulting to data loss.
"""

from __future__ import annotations

from typing import Any

# Settings ESPN does not know about, and cannot give back.
CARRIED_FORWARD: tuple[str, ...] = (
    "managers",            # hand-typed nicknames; merged, not copied — see nicknames.carry_forward
    "aliases",             # two accounts, one human; declared by hand and unrecoverable
    "reference_path",
    "baseline_teams",
    "baseline_budget",
    "poll_interval_seconds",
    "poll_failure_threshold",
)

# Everything ESPN is authoritative about, which a rebuild is *supposed* to
# replace. Named explicitly so the coverage test can tell "deliberately
# refreshed" from "forgotten".
FROM_ESPN: tuple[str, ...] = (
    "league_id", "year", "private", "name",
    "draft_type", "budget",
    "team_count", "my_team_id", "team_ids",
    "roster", "flex_positions", "scoring_type",
    "owners", "team_names", "real_names",
)


def carried(previous) -> dict[str, Any]:
    """The keyword arguments that preserve a previous config's local choices."""
    return {name: getattr(previous, name) for name in CARRIED_FORWARD}
