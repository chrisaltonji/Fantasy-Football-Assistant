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
from typing import Mapping

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


def _slug(text: str) -> str:
    """`"O'Brien"` -> `obrien`. Lower case, letters and digits only."""
    return "".join(c for c in (text or "").lower() if c.isalnum())


def derive_nicknames(
    real_names: Mapping[int, str],
    fallbacks: Mapping[int, str] | None = None,
) -> dict[int, str]:
    """First names, made unique and typeable.

    ESPN's `mTeam` view carries `firstName`/`lastName` for every member *and* an
    account `displayName` like `macurl1392`. Preferring the username was a plain
    ordering mistake: the real names were there the whole time, and a nickname
    exists to be typed by a person under a clock.

    A full name cannot be the nickname — the command grammar splits on
    whitespace, so `sold barkley 62 michael curley` parses `curley` as a
    separate token. So this takes the first name, and escalates only as far as
    it has to:

    1. `michael`
    2. on a collision, add the last initial — this league has two Nicks and two
       Andrews, and `nick` silently resolving to whichever came first is exactly
       the ambiguity `resolve_team` refuses to guess at
    3. still colliding, the whole last name
    4. still no good, whatever `fallbacks` offers (ESPN's username), which is
       ugly but unique and typeable

    A candidate that cannot survive `check_nickname` is dropped rather than
    written: a config entry that never resolves is worse than none, because the
    team stays addressable as `t3` either way and only one of the two is honest
    about it.
    """
    fallbacks = fallbacks or {}
    parts = {
        team_id: [_slug(p) for p in str(name).split() if _slug(p)]
        for team_id, name in real_names.items()
    }

    def candidate(team_id: int, level: int) -> str:
        words = parts.get(team_id) or []
        if not words:
            return ""
        first, rest = words[0], words[1:]
        if level == 0 or not rest:
            return first
        if level == 1:
            return first + rest[-1][0]
        return first + rest[-1]

    chosen: dict[int, str] = {}
    unresolved = set(parts)

    for level in (0, 1, 2):
        proposals: dict[int, str] = {}
        for team_id in sorted(unresolved):
            name = candidate(team_id, level)
            try:
                proposals[team_id] = check_nickname(name)
            except ValueError:
                continue

        counts: dict[str, int] = {}
        for name in proposals.values():
            counts[name.lower()] = counts.get(name.lower(), 0) + 1

        for team_id, name in proposals.items():
            taken = {v.lower() for v in chosen.values()}
            if counts[name.lower()] == 1 and name.lower() not in taken:
                chosen[team_id] = name
                unresolved.discard(team_id)

    for team_id in sorted(unresolved):
        fallback = fallbacks.get(team_id, "")
        try:
            name = check_nickname(fallback)
        except ValueError:
            continue
        if name.lower() not in {v.lower() for v in chosen.values()}:
            chosen[team_id] = name

    return chosen


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
    machine_names: Mapping[int, "set[str] | tuple[str, ...]"] | None = None,
) -> tuple[dict[int, str], list[str]]:
    """Keep hand-set nicknames across a `config init --force`.

    Regenerating the config is the documented fix for a lost or stale
    ``league.toml``, so it must not quietly undo the one thing in that file
    nobody can reconstruct from ESPN. Three cases, and the last two are why this
    is not just ``previous | fresh``:

    - A hand-set nickname is kept. That is the whole point.
    - A nickname ESPN supplied is not kept. `looks_generated` catches the
      `espn06814226` shape, and `machine_names` catches the rest: an account
      username like `macurl1392` was never typed by anyone, so preserving it
      would pin the config to a worse name than the one now on offer and there
      would be no way to escape it short of editing the file by hand.
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
        supplied = {n.lower() for n in (machine_names or {}).get(team_id, ())}
        if looks_generated(nickname) or nickname.lower() in supplied:
            continue

        # Resolved, not raw. `{ABC}` and `ABC` are the same seat, and an
        # aliased second account is the same person — either would otherwise
        # read as a new manager and throw the nickname away.
        from ffa.config.identity import fold_owner_id

        before = fold_owner_id(previous_owners.get(team_id, ""))
        after = fold_owner_id(fresh_owners.get(team_id, ""))
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
