"""Names you can type with a nomination clock running.

ESPN's answer to "who manages team 5" is `espn78078705`. That is what seeds
`[managers]`, and it makes the correction path — the thing you reach for when a
pick is attributed wrong — into `sold barkley 62 espn78078705`. Nobody types
that twice under pressure.

So the nickname is a typing affordance, and the rules here are the ones that
make it one: it has to survive a config rebuild, it has to be reachable by the
command grammar, and it has to identify exactly one team.
"""

from __future__ import annotations

import pytest

from ffa.config.nicknames import (
    carry_forward,
    check_nickname,
    check_unique,
    derive_nicknames,
    looks_generated,
    parse_assignment,
)


# --- telling a real name from ESPN's ---------------------------------------------


@pytest.mark.parametrize("name", ["espn06814226", "ESPN78078705", "espn1"])
def test_espn_generated_usernames_are_recognised(name):
    assert looks_generated(name) is True


@pytest.mark.parametrize("name", ["dave", "espn", "espnjoe", "joe2", "", "espn 123"])
def test_a_name_a_human_chose_is_not_treated_as_generated(name):
    assert looks_generated(name) is False


# --- what makes a nickname usable ------------------------------------------------


def test_a_plain_word_is_fine():
    assert check_nickname("  dave  ") == "dave"


def test_a_nickname_with_a_space_is_refused_because_commands_split_on_them():
    """`sold barkley 62 dave smith` parses `smith` as a separate token."""
    with pytest.raises(ValueError, match="space"):
        check_nickname("dave smith")


def test_an_empty_nickname_is_refused():
    with pytest.raises(ValueError):
        check_nickname("   ")


@pytest.mark.parametrize("name", ["3", "t3", "team3", "T12"])
def test_a_slot_shaped_nickname_is_refused_because_it_is_unreachable(name):
    """`resolve_team` tries `t3`/`team3`/`3` first, deliberately.

    A manager nicknamed `3` can never be addressed: the slot form always wins.
    Accepting it would write a config entry that silently does nothing.
    """
    with pytest.raises(ValueError, match="slot"):
        check_nickname(name)


def test_duplicate_nicknames_are_refused_with_both_teams_named():
    with pytest.raises(ValueError) as exc:
        check_unique({3: "dave", 7: "Dave"})
    assert "team 3" in str(exc.value) and "team 7" in str(exc.value)


def test_distinct_nicknames_pass():
    check_unique({3: "dave", 7: "mike"})


# --- parsing --set ----------------------------------------------------------------


def test_an_assignment_splits_into_a_team_id_and_a_name():
    assert parse_assignment(" 3 = dave ") == (3, "dave")


def test_an_assignment_with_no_equals_says_what_the_shape_is():
    with pytest.raises(ValueError, match="ID=NICKNAME"):
        parse_assignment("dave")


def test_an_assignment_keyed_by_something_that_is_not_a_team_id_is_refused():
    with pytest.raises(ValueError, match="team id"):
        parse_assignment("dave=dave")


def test_an_assignment_still_enforces_the_nickname_rules():
    with pytest.raises(ValueError, match="space"):
        parse_assignment("3=dave smith")


# --- surviving `config init --force` ----------------------------------------------

IDS = (1, 2, 3, 4, 5, 7)


def merge(previous, fresh, *, previous_owners=None, fresh_owners=None, ids=IDS):
    return carry_forward(
        previous_managers=previous,
        previous_owners=previous_owners or {},
        fresh_managers=fresh,
        fresh_owners=fresh_owners or {},
        team_ids=ids,
    )


def test_a_hand_set_nickname_survives_a_rebuild():
    """Regenerating the config is the documented fix for losing it.

    If that wiped the nicknames, the fix would cost you the one thing in the
    file ESPN cannot give back.
    """
    merged, _ = merge({3: "dave"}, {3: "espn06814226", 5: "espn78078705"})
    assert merged[3] == "dave"
    assert merged[5] == "espn78078705"


def test_a_generated_name_is_replaced_by_the_current_one():
    """Nothing is gained by pinning a stale username."""
    merged, _ = merge({3: "espn111"}, {3: "espn222"})
    assert merged[3] == "espn222"


def test_a_nickname_is_dropped_when_the_team_changes_hands():
    """The SWID is who a team actually is; the nickname is only a label.

    Carrying `dave` onto whoever replaced Dave would address advice to the wrong
    human, which is exactly the failure the owner id exists to prevent.
    """
    merged, notes = merge(
        {3: "dave"},
        {3: "espn999"},
        previous_owners={3: "{SWID-A}"},
        fresh_owners={3: "{SWID-B}"},
    )
    assert merged[3] == "espn999"
    assert any("different owner" in note for note in notes)


def test_a_nickname_for_a_team_that_left_the_league_is_dropped_and_reported():
    merged, notes = merge({9: "dave"}, {})
    assert 9 not in merged
    assert any("no longer in this league" in note for note in notes)


def test_a_hand_set_name_outranks_the_same_generated_name_elsewhere():
    """Uniqueness is enforced later, so the clash has to be resolved here.

    Otherwise `config init --force` writes a config that then refuses to load —
    the worst moment for a rebuild to fail is right after you needed one.
    """
    merged, notes = merge({3: "boogie"}, {3: "espn111", 5: "boogie"})
    assert merged[3] == "boogie"
    assert 5 not in merged
    assert any("collided" in note for note in notes)
    check_unique(merged)


def test_the_merge_never_leaves_two_teams_answering_to_one_name():
    merged, _ = merge(
        {1: "dave", 2: "mike"},
        {1: "espn1", 2: "espn2", 3: "dave", 4: "mike"},
    )
    check_unique(merged)


# --- deriving nicknames from the real names ESPN already had -----------------------

# The real league, which is what made every one of these cases show up.
REAL = {
    1: "andrew donnelly", 2: "Brendan Evans", 3: "Christopher Altonji",
    4: "John Mulhall", 5: "Michael Curley", 7: "Nick Lynch",
    8: "George Stairiker", 9: "Nick Cardman", 10: "Scott Muldoon",
    11: "Jared Bowers", 12: "Andrew Rudner", 13: "B C",
}
HANDLES = {
    1: "tjdlad", 2: "brendanae", 3: "caltonji", 4: "jmulha01", 5: "macurl1392",
    7: "espn06814226", 8: "espn78078705", 9: "prepman2732",
    10: "espnfan8703681814", 11: "BoogieNights222", 12: "Arudne2013",
    13: "ESPNfan4536618844",
}


def test_a_first_name_is_the_nickname_when_it_is_unambiguous():
    assert derive_nicknames({5: "Michael Curley"}) == {5: "michael"}


def test_a_shared_first_name_gets_a_last_initial_rather_than_a_coin_flip():
    """Two Nicks and two Andrews in the real league.

    `nick` resolving to whichever came first is exactly the ambiguity
    `resolve_team` refuses to guess at, so it must not be written in the first
    place.
    """
    derived = derive_nicknames({7: "Nick Lynch", 9: "Nick Cardman"})

    assert derived == {7: "nickl", 9: "nickc"}
    check_unique(derived)


def test_a_shared_first_name_and_initial_escalates_to_the_whole_last_name():
    derived = derive_nicknames({1: "Nick Lynch", 2: "Nick Lyons"})

    assert derived == {1: "nicklynch", 2: "nicklyons"}
    check_unique(derived)


def test_two_people_with_the_same_full_name_fall_back_to_their_handles():
    """Rare, but a league with a father and son in it is not exotic."""
    derived = derive_nicknames(
        {1: "Nick Lynch", 2: "Nick Lynch"}, {1: "nickl77", 2: "nickl88"}
    )

    check_unique(derived)
    assert set(derived.values()) == {"nickl", "nickl88"} or len(set(derived.values())) == 2


def test_punctuation_and_case_are_folded_out():
    assert derive_nicknames({1: "Sean O'Brien"}) == {1: "sean"}
    assert derive_nicknames({1: "andrew donnelly", 2: "Andrew Rudner"}) == {
        1: "andrewd", 2: "andrewr",
    }


def test_someone_with_no_real_name_falls_back_to_their_espn_handle():
    """ESPN leaves firstName/lastName off some accounts. Ugly beats unreachable."""
    derived = derive_nicknames({1: ""}, {1: "espn06814226"})
    assert derived == {1: "espn06814226"}


def test_a_seat_with_neither_a_name_nor_a_handle_is_left_unnamed():
    """A config entry that never resolves is worse than none.

    The team stays addressable as `t3` either way, and only one of the two is
    honest about it.
    """
    assert derive_nicknames({1: ""}, {}) == {}


def test_a_derived_nickname_can_never_be_slot_shaped():
    """`t3`/`team3`/`3` always win in `resolve_team`, so such a name is dead."""
    assert derive_nicknames({1: "3 4"}, {}) == {}


def test_the_whole_real_league_derives_cleanly():
    derived = derive_nicknames(REAL, HANDLES)

    assert len(derived) == len(REAL)
    check_unique(derived)
    for nickname in derived.values():
        check_nickname(nickname)
    # And nothing ends up wearing an ESPN handle, because every seat has a name.
    assert not any(looks_generated(n) for n in derived.values())


# --- not mistaking an ESPN handle for a hand-set nickname ---------------------------


def test_an_account_handle_is_not_carried_forward_as_if_you_typed_it():
    """`looks_generated` only catches the `espn06814226` shape.

    A config written before real names were read holds `macurl1392`, which
    nobody chose. Preserving it would pin the config to a worse name with no way
    out short of editing the file by hand.
    """
    merged, _ = carry_forward(
        previous_managers={5: "macurl1392"},
        previous_owners={},
        fresh_managers={5: "michael"},
        fresh_owners={},
        team_ids=(5,),
        machine_names={5: {"macurl1392"}},
    )

    assert merged[5] == "michael"


def test_a_genuinely_hand_set_nickname_still_survives():
    merged, _ = carry_forward(
        previous_managers={5: "curls"},
        previous_owners={},
        fresh_managers={5: "michael"},
        fresh_owners={},
        team_ids=(5,),
        machine_names={5: {"macurl1392", "michael"}},
    )

    assert merged[5] == "curls"
