"""Manager nicknames: the string you type when you correct a pick.

ESPN's own answer to "who is this manager" is a display name like
``espn06814226``. It is a perfectly good identifier and a terrible thing to type
with a nomination clock running — ``sold barkley 62 espn78078705`` is not a
command anyone gets right twice. So the nickname in ``[managers]`` is a *typing
affordance*, hand-set once, and everything here exists to keep it that way.

Two rules follow from how `resolve_team` reads them, and both are enforced here
rather than discovered mid-draft:

- **No whitespace.** The command grammar splits on it, so ``dave smith`` parses
  as two tokens and the team never resolves.
- **Not a slot form.** ``resolve_team`` tries ``t3``/``team3``/``3`` first, on
  purpose, so a manager nicknamed ``3`` is simply unreachable.

Pure: no I/O, no config loading. The CLI wraps it.
"""

from __future__ import annotations

import re

# ESPN's placeholder display name for a member who never set one.
_GENERATED = re.compile(r"^espn\d+$", re.IGNORECASE)

# Mirrors `ffa.ingest.manual.resolve._SLOT_TOKEN`. Kept as its own pattern
# rather than imported: this module is about config, and a config rule that
# silently changed because a parser was refactored would be worse than the
# duplication.
_SLOT_FORM = re.compile(r"^(?:t|team)?\d+$", re.IGNORECASE)


def looks_generated(nickname: str) -> bool:
    """True for a name ESPN made up, false for one a human chose."""
    return bool(_GENERATED.match((nickname or "").strip()))


def check_nickname(nickname: str) -> str:
    """Return the cleaned nickname, or raise ValueError saying why not."""
    text = (nickname or "").strip()
    if not text:
        raise ValueError("a nickname cannot be empty")
    if any(c.isspace() for c in text):
        raise ValueError(
            f"{text!r} has a space in it. Commands split on whitespace, so this "
            "would never resolve — try one word."
        )
    if _SLOT_FORM.match(text):
        raise ValueError(
            f"{text!r} looks like a team slot. `t3`/`team3`/`3` always resolve "
            "to team 3 first, so a manager named that is unreachable."
        )
    return text


def parse_assignment(text: str) -> tuple[int, str]:
    """``"3=dave"`` -> ``(3, "dave")``."""
    raw = (text or "").strip()
    team_part, sep, name_part = raw.partition("=")
    if not sep:
        raise ValueError(f"expected ID=NICKNAME, got {raw!r}. Example: 3=dave")
    try:
        team_id = int(team_part.strip())
    except ValueError:
        raise ValueError(
            f"{team_part.strip()!r} is not an ESPN team id. Example: 3=dave"
        ) from None
    return team_id, check_nickname(name_part)


def check_unique(managers: dict[int, str]) -> None:
    """Nicknames have to be typeable *unambiguously*, so duplicates are fatal."""
    seen: dict[str, int] = {}
    clashes: list[str] = []
    for team_id, nickname in sorted(managers.items()):
        folded = nickname.lower()
        if folded in seen:
            clashes.append(f"{nickname!r} is on both team {seen[folded]} and team {team_id}")
        else:
            seen[folded] = team_id
    if clashes:
        raise ValueError("; ".join(clashes))


def carry_forward(
    *,
    previous_managers: dict[int, str],
    previous_owners: dict[int, str],
    fresh_managers: dict[int, str],
    fresh_owners: dict[int, str],
    team_ids: tuple[int, ...],
) -> tuple[dict[int, str], list[str]]:
    """Keep hand-set nicknames across a `config init --force`.

    Regenerating the config is the documented fix for a lost or stale
    ``league.toml``, so it must not quietly undo the one thing in that file
    nobody can reconstruct from ESPN. Three cases, and the last two are why this
    is not just ``previous | fresh``:

    - A hand-set nickname is kept. That is the whole point.
    - A nickname that *is* just ESPN's generated username is not kept — the
      fresh one is equally good and is current.
    - A nickname whose team changed hands is dropped. The owner SWID is the
      anchor for who a team actually is; carrying ``dave`` onto the manager who
      replaced Dave would produce advice addressed to the wrong human, which is
      exactly the failure the SWID exists to prevent.

    Returns the merged mapping plus notes worth printing.
    """
    merged = {
        team_id: nickname
        for team_id, nickname in fresh_managers.items()
        if team_id in team_ids
    }
    notes: list[str] = []

    for team_id, nickname in sorted(previous_managers.items()):
        if team_id not in team_ids:
            notes.append(
                f"dropped nickname {nickname!r}: team {team_id} is no longer in "
                "this league"
            )
            continue
        if looks_generated(nickname):
            continue

        before, after = previous_owners.get(team_id), fresh_owners.get(team_id)
        if before and after and before != after:
            notes.append(
                f"dropped nickname {nickname!r}: team {team_id} has a different "
                "owner now, so the name would point at the wrong person"
            )
            continue

        # A hand-set name outranks whatever ESPN currently calls that member,
        # including when ESPN happens to call somebody *else* the same thing.
        for other_id, other in list(merged.items()):
            if other_id != team_id and other.lower() == nickname.lower():
                del merged[other_id]
                notes.append(
                    f"team {other_id} lost its generated name {other!r}: it "
                    f"collided with your nickname for team {team_id}"
                )
        merged[team_id] = nickname

    return merged, notes
