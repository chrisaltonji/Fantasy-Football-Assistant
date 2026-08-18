"""Who a manager *is*, across accounts and across seasons.

Everything in this codebase anchors a manager on their ESPN owner SWID, because
team names change mid-draft and team ids get reshuffled between seasons. That
holds right up until somebody drafts under a second account, which is not exotic:
this league has two cases already. Brian Cona held team 6 through 2023 under
`espn99240290` and came back at team 13 in 2024 as `ESPNfan4536618844`, and there
is an orphan `Andrew Donnelly` account with no seat at all.

Without an alias table those are two people. The record splits into two
half-length profiles, every metric is computed on half the evidence, and nothing
reports a problem — the arithmetic is perfectly correct about somebody who does
not exist.

**Aliases are declared, never inferred.** Two accounts with similar names are not
evidence of anything, and guessing at identity is exactly what the rest of this
codebase refuses to do — `resolve_team` will not guess between two managers,
`PlayerBook` will not guess between two players. The user asserts the link; this
module applies it consistently.

Three levels, and they are different things:

- `fold_owner_id` — punctuation. `{abc}` and `ABC` are one string written two
  ways. Always safe, never a judgement.
- `OwnerResolver.resolve` — identity. Two genuinely different ids are one human,
  because somebody said so.
- `seats` — seating. Which resolved human is in which chair *this* season.

**The canonical id of a group is the account that currently holds a seat.** That
rule is what makes the table robust to being written backwards: `A = B` and
`B = A` mean the same thing, because the seated one wins either way. Everything
downstream — dossiers, suggestions, precedent — is keyed by seat in the end, so
canonicalising to anything else would resolve a manager to an id that no command
can look up.
"""

from __future__ import annotations

from typing import Iterable, Mapping

# An alias chain should be one or two hops. A longer walk means the table is
# wrong, and following it forever would hang a draft rather than fail one.
MAX_HOPS = 8


def fold_owner_id(swid: str | None) -> str:
    """The canonical form of a SWID: no braces, upper case.

    ESPN hands the same member id out both ways — `{ABC-123}` from `mTeam`,
    `ABC-123` from a pasted cookie — and users copy whichever they saw. Folding
    at *construction* rather than only at lookup is what stops a file growing
    two entries for one person: two keys that differ only by punctuation, both
    valid, one of them silently shadowing the other after a sort.
    """
    return (swid or "").strip().strip("{}").upper()


def _groups(aliases: Mapping[str, str]) -> list[set[str]]:
    """Every declared equivalence, as connected components.

    Union-find rather than chain-following, because a cycle is then structurally
    impossible to loop on: `A→B, B→A` is simply one group of two, and `A→B→C→A`
    one group of three. A hand-written table *will* eventually contain a cycle,
    and it should be an odd-looking group rather than an error.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        seen = 0
        while parent[x] != x and seen < MAX_HOPS * 4:
            parent[x] = parent[parent[x]]
            x = parent[x]
            seen += 1
        return x

    for raw_a, raw_b in aliases.items():
        a, b = fold_owner_id(raw_a), fold_owner_id(raw_b)
        if not a or not b or a == b:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    out: dict[str, set[str]] = {}
    for member in list(parent):
        out.setdefault(find(member), set()).add(member)
    return list(out.values())


class OwnerResolver:
    """An alias table, applied.

    Carried rather than passed as a bare dict so a caller cannot forget to fold
    on one of the ten joins that key on a SWID — the failure that motivated this
    module is precisely a join that looked right and silently split a person.

    `owners` is the current `team_id -> SWID` map. Passing it is what lets the
    seated account win, which is why an alias written in either direction gives
    the same answer.
    """

    def __init__(
        self,
        aliases: Mapping[str, str] | None = None,
        owners: Mapping[int, str] | None = None,
    ) -> None:
        self._aliases = dict(aliases or {})
        self._seated = {fold_owner_id(s) for s in (owners or {}).values() if s}
        self.warnings: list[str] = []

        self._canonical: dict[str, str] = {}
        for group in _groups(self._aliases):
            seated = sorted(group & self._seated)
            if len(seated) > 1:
                self.warnings.append(
                    "aliases claim "
                    + " and ".join(seated)
                    + " are one person, but both hold a seat this season — one of "
                    "them is wrong, and picks would be credited to the wrong "
                    "manager. Fix with `ffa config alias --remove`."
                )
            primary = seated[0] if seated else sorted(group)[0]
            if not seated:
                self.warnings.append(
                    f"no account in the alias group {sorted(group)} holds a seat "
                    "this season — using " + primary
                )
            for member in group:
                self._canonical[member] = primary

    def resolve(self, swid: str | None) -> str:
        folded = fold_owner_id(swid)
        return self._canonical.get(folded, folded)

    def group(self, swid: str | None) -> set[str]:
        """Every account id that resolves to the same person as this one."""
        primary = self.resolve(swid)
        if not primary:
            return set()
        return {m for m, p in self._canonical.items() if p == primary} | {primary}

    def is_alias(self, swid: str | None) -> bool:
        folded = fold_owner_id(swid)
        return bool(folded) and self.resolve(folded) != folded

    @property
    def aliases(self) -> Mapping[str, str]:
        return dict(self._aliases)

    def __bool__(self) -> bool:
        return bool(self._canonical)

    def __len__(self) -> int:
        return len(self._aliases)

    @classmethod
    def from_config(cls, config) -> "OwnerResolver":
        return cls(getattr(config, "aliases", None), getattr(config, "owners", None))


def seats(config) -> dict[int, str]:
    """`team_id -> resolved owner`, for every seat we can identify this season.

    The one implementation. Three copies of this existed — in `dossier_cmd`, in
    `history_cmd`, and inline in `app.py` — and they had already drifted: two
    filtered out teams with no recorded owner and the third did not, so the same
    league produced different seat maps depending on which command you ran.

    A team with no owner is omitted rather than given a placeholder. There is
    nothing stable to attach to it, and saying so beats inventing a key that
    breaks the moment somebody leaves the league.
    """
    resolver = OwnerResolver.from_config(config)
    return {
        team_id: resolver.resolve(config.owners[team_id])
        for team_id in config.effective_team_ids
        if config.owners.get(team_id)
    }


def richest_name(names: Iterable[str]) -> str:
    """The most informative of several names for one person.

    Aliased accounts carry different names for the same human — ESPN has this
    league's Brian Cona as `Brian Cona` on one account and `B C` on the other.
    Neither is wrong, but only one is usable, so the one with the most letters
    wins. Ties go to the longer string, then alphabetically, so the choice does
    not wobble between runs.
    """
    real = [n.strip() for n in names if n and n.strip()]
    if not real:
        return ""
    return max(real, key=lambda n: (sum(c.isalpha() for c in n), len(n), n))
