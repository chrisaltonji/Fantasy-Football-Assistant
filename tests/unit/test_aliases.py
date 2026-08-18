"""Two ESPN accounts, one human.

Every join in this codebase keys on an owner SWID, which is right until somebody
drafts under a second login. Then the record splits, every metric is computed on
half the evidence, and **nothing reports a problem** — the arithmetic is
perfectly correct about a person who does not exist. This league has two cases
already.

The rules being pinned here: an alias is declared and never inferred; the
*seated* account is canonical, so declaring it backwards is harmless; a cycle
warns rather than hangs; and a rebuild does not throw the table away.
"""

from __future__ import annotations

import pytest

from ffa.config.carry import CARRIED_FORWARD, FROM_ESPN, carried
from ffa.config.identity import (
    OwnerResolver,
    fold_owner_id,
    richest_name,
    seats,
)
from ffa.config.schema import LeagueConfig

BRIAN = "E6051A85-3DB4-419F-837C-4BC7F557A5BB"   # team 6, 2022-2023
B_C = "32B47D23-4C45-4B59-B9B0-D9DCAFB6EEAF"     # team 13, 2024-
OTHER = "781E77E6-38F7-4D31-9F9C-FAD22A387F75"


def config(**overrides) -> LeagueConfig:
    base = dict(
        league_id=1, year=2026, team_count=3, team_ids=(1, 2, 13),
        owners={1: "{" + OTHER + "}", 13: "{" + B_C + "}"},
    )
    base.update(overrides)
    return LeagueConfig(**base)


# --- folding is punctuation, not judgement ------------------------------------------


def test_folding_collapses_the_two_ways_espn_writes_a_swid():
    assert fold_owner_id("{abc-123}") == fold_owner_id("ABC-123") == "ABC-123"


def test_folding_is_idempotent():
    once = fold_owner_id("{abc}")
    assert fold_owner_id(once) == once


def test_an_unaliased_id_resolves_to_itself():
    """The default has to be a strict no-op, or every seam changes behaviour."""
    assert OwnerResolver().resolve("{abc}") == "ABC"


# --- the seated account is canonical -------------------------------------------------


def test_the_seated_account_wins_however_the_alias_was_written():
    """`A = B` and `B = A` mean the same thing, so both must resolve the same way.

    Everything downstream is keyed by seat, so canonicalising to the departed
    account would resolve a manager to an id no command can look up.
    """
    forward = OwnerResolver({BRIAN: B_C}, {13: B_C})
    backward = OwnerResolver({B_C: BRIAN}, {13: B_C})

    assert forward.resolve(BRIAN) == B_C
    assert backward.resolve(BRIAN) == B_C
    assert backward.resolve(B_C) == B_C


def test_a_group_with_nobody_seated_still_resolves_deterministically():
    resolver = OwnerResolver({BRIAN: "ZZZ"}, {13: B_C})

    assert resolver.resolve(BRIAN) == resolver.resolve("ZZZ")
    assert any("holds a seat" in w for w in resolver.warnings)


def test_two_seated_accounts_cannot_be_one_person_and_it_says_so():
    """Both hold a chair, so picks would be credited to the wrong manager."""
    resolver = OwnerResolver({BRIAN: B_C}, {6: BRIAN, 13: B_C})

    assert any("both hold a seat" in w for w in resolver.warnings)


# --- chains and cycles ----------------------------------------------------------------


def test_a_chain_of_accounts_collapses_to_one_person():
    resolver = OwnerResolver({"A": "B", "B": "C"}, {1: "C"})

    assert resolver.resolve("A") == resolver.resolve("B") == "C"


def test_a_cycle_warns_rather_than_hanging():
    """A hand-written table will eventually contain one. Union-find makes it a
    group of two rather than a walk with no end."""
    resolver = OwnerResolver({"A": "B", "B": "A"}, {1: "B"})

    assert resolver.resolve("A") == resolver.resolve("B") == "B"


def test_a_self_alias_is_ignored_rather_than_treated_as_a_group():
    assert OwnerResolver({"A": "A"}).resolve("A") == "A"


def test_the_group_lists_every_account_for_one_person():
    resolver = OwnerResolver({BRIAN: B_C}, {13: B_C})
    assert resolver.group(B_C) == {BRIAN, B_C}


def test_is_alias_marks_only_the_secondary():
    resolver = OwnerResolver({BRIAN: B_C}, {13: B_C})
    assert resolver.is_alias(BRIAN) is True
    assert resolver.is_alias(B_C) is False


# --- seating --------------------------------------------------------------------------


def test_seats_resolve_through_the_alias_table():
    mapping = seats(config(aliases={BRIAN: B_C}))
    assert mapping == {1: OTHER, 13: B_C}


def test_a_team_with_no_owner_is_omitted_rather_than_given_a_placeholder():
    """There is nothing stable to attach to it, and saying so beats inventing a
    key that breaks the moment somebody leaves the league."""
    mapping = seats(config(team_ids=(1, 2, 13)))
    assert 2 not in mapping


# --- picking a name across accounts ---------------------------------------------------


def test_the_more_informative_name_wins():
    """ESPN has this person as `Brian Cona` on one account and `B C` on the other."""
    assert richest_name(["B C", "Brian Cona"]) == "Brian Cona"
    assert richest_name(["", "  "]) == ""


def test_name_choice_does_not_wobble_between_runs():
    assert richest_name(["Ann Lee", "Bob Fox"]) == richest_name(["Bob Fox", "Ann Lee"])


# --- surviving a rebuild ---------------------------------------------------------------


def test_every_config_field_has_a_decision_about_a_rebuild():
    """`config init --force` used to hardcode six field names, so every new
    setting was silently wiped by the documented recovery command until somebody
    noticed. This is the test that makes adding a field force a decision."""
    fields = set(LeagueConfig.__dataclass_fields__)
    uncategorised = fields - set(CARRIED_FORWARD) - set(FROM_ESPN)

    assert not uncategorised, (
        f"{sorted(uncategorised)} is in neither CARRIED_FORWARD nor FROM_ESPN — "
        "decide whether a rebuild should keep it or take ESPN's version"
    )


def test_aliases_are_carried_forward_across_a_rebuild():
    """ESPN knows nothing about aliases, so a rebuild would otherwise split a
    manager back into two — the exact bug, reintroduced by the fix for it."""
    previous = config(aliases={BRIAN: B_C})
    assert carried(previous)["aliases"] == {BRIAN: B_C}


def test_the_two_lists_do_not_overlap():
    assert not set(CARRIED_FORWARD) & set(FROM_ESPN)


# --- config round trip ------------------------------------------------------------------


def test_aliases_survive_a_write_and_reload(tmp_path):
    from ffa.config.loader import load_config, write_config

    path = tmp_path / "league.toml"
    write_config(
        LeagueConfig(
            league_id=1, year=2026, team_count=2, team_ids=(1, 13),
            my_team_id=1, owners={1: OTHER, 13: B_C}, aliases={BRIAN: B_C},
        ),
        path,
    )
    assert load_config(path).aliases == {BRIAN: B_C}


def test_a_hand_written_alias_is_folded_on_the_way_in(tmp_path):
    """Somebody will paste one form and type the other."""
    import tomli_w

    from ffa.config.loader import load_config

    path = tmp_path / "league.toml"
    path.write_bytes(tomli_w.dumps({
        "league": {"league_id": 1, "year": 2026},
        "teams": {"count": 2, "ids": [1, 13], "my_team_id": 1},
        "roster": {"QB": 1, "RB": 1},
        "owners": {"1": OTHER, "13": B_C},
        "aliases": {"{" + BRIAN.lower() + "}": "{" + B_C + "}"},
    }).encode("utf-8"))

    assert load_config(path).aliases == {BRIAN: B_C}


def test_an_alias_missing_a_side_is_refused(tmp_path):
    from ffa.config.loader import parse_config
    from ffa.config.schema import ConfigError

    with pytest.raises(ConfigError, match="SWID"):
        parse_config({
            "league": {"league_id": 1, "year": 2026},
            "teams": {"count": 1, "ids": [1], "my_team_id": 1},
            "roster": {"QB": 1},
            "aliases": {BRIAN: ""},
        })


# --- the seams that would otherwise split a person ---------------------------------------


def test_my_team_id_still_resolves_when_your_own_account_is_the_alias():
    from ffa.ingest.espn.settings import my_team_id

    owners = {13: B_C}
    assert my_team_id(owners, BRIAN, {BRIAN: B_C}) == 13


def test_my_team_id_matches_a_lower_case_swid():
    """The old local fold omitted `.upper()`, so a SWID copied out of a browser
    silently failed to match and left `my_team_id` at 0."""
    from ffa.ingest.espn.settings import my_team_id

    assert my_team_id({3: "{" + OTHER + "}"}, OTHER.lower()) == 3


def test_a_nickname_survives_a_rebuild_when_the_seat_swid_changes_form():
    """`{A}` and `A` are the same seat; an unfolded compare read it as a new
    manager and threw the hand-typed nickname away."""
    from ffa.config.nicknames import carry_forward

    merged, _ = carry_forward(
        previous_managers={13: "b"},
        previous_owners={13: "{" + B_C + "}"},
        fresh_managers={13: "espn123"},
        fresh_owners={13: B_C},
        team_ids=(13,),
    )
    assert merged[13] == "b"


def test_two_dossier_entries_for_one_person_fold_without_losing_answers():
    from ffa.dossier.schema import Skill, seed
    from dataclasses import replace as _replace
    from ffa.dossier.store import DossierBook

    resolver = OwnerResolver({BRIAN: B_C}, {13: B_C})
    primary = _replace(seed(B_C, team_id=13), skill=Skill.SHARP)
    secondary = _replace(seed(BRIAN), real_name="Brian Cona")

    book = DossierBook(
        {B_C: primary, BRIAN: secondary}, owners={13: B_C}, resolver=resolver
    )

    assert len(book) == 1
    assert book.for_team(13).skill is Skill.SHARP


def test_contradictory_answers_across_accounts_are_reported_not_merged():
    """A dossier records what somebody told us. Quietly picking one of two
    conflicting answers would fabricate an observation."""
    from dataclasses import replace as _replace

    from ffa.dossier.schema import Skill, seed
    from ffa.dossier.store import DossierBook

    resolver = OwnerResolver({BRIAN: B_C}, {13: B_C})
    book = DossierBook(
        {
            B_C: _replace(seed(B_C, team_id=13), skill=Skill.SHARP),
            BRIAN: _replace(seed(BRIAN), skill=Skill.CASUAL),
        },
        owners={13: B_C},
        resolver=resolver,
    )

    assert book.for_team(13).skill is Skill.SHARP
    assert any("both answer" in w for w in book.warnings)
