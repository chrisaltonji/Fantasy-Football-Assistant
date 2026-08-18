"""Owner dossiers: the record, the questions, and the file.

Everything else in this codebase is capacity — can this rival afford him, does
he have a slot. A dossier is intent, which cannot be computed, only observed.
So the tests here are mostly about *not losing* what somebody told us: the
anchor surviving a rename, an unreadable line not costing the whole file, and
"never asked" staying distinguishable from "asked, nothing remarkable".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffa.domain.enums import Position
from ffa.dossier.schema import (
    BY_FIELD,
    QUESTIONS,
    Chases,
    DossierError,
    OwnerDossier,
    Skill,
    SpendShape,
    fold_owner_id,
    from_dict,
    parse_answer,
    render_answer,
    seed,
    to_dict,
)
from ffa.dossier.store import DossierBook, load_dossiers, save_dossiers

SWID_A = "{OWNER-01}"
SWID_B = "{OWNER-02}"


def filled(owner_id: str = SWID_A, **overrides) -> OwnerDossier:
    base = dict(
        team_id=3,
        label="dave",
        real_name="Dave",
        skill=Skill.SHARP,
        spend_shape=SpendShape.STARS_AND_SCRUBS,
        chases=Chases.OFTEN,
        overpays_at=(Position.QB,),
        homer_teams=("KC",),
        tells="goes quiet when he is out of money",
    )
    base.update(overrides)
    return OwnerDossier(owner_id=fold_owner_id(owner_id), **base)


# --- the anchor ------------------------------------------------------------------


def test_the_swid_is_folded_so_one_person_cannot_become_two_entries():
    """ESPN hands the same member id out braced and bare, and users copy either.

    Two keys differing only by punctuation are both valid, and after a sort one
    silently shadows the other — which is exactly how a hand-edited answer
    disappears.
    """
    assert fold_owner_id("{owner-01}") == fold_owner_id("OWNER-01") == "OWNER-01"
    assert seed("{OWNER-01}").owner_id == seed("owner-01").owner_id


def test_a_dossier_is_found_by_whichever_form_of_the_swid_you_have():
    book = DossierBook({SWID_A: filled()}, owners={3: "OWNER-01"})

    assert book.for_owner("{OWNER-01}") is not None
    assert book.for_owner("owner-01") is not None
    assert book.for_team(3) is not None


def test_the_config_decides_who_sits_where_not_the_file():
    """The `team_id` inside a dossier is display text from whenever it was written.

    Trusting it would attribute last season's read to whoever inherited the seat
    — the precise failure the SWID anchor exists to prevent.
    """
    stale = filled(team_id=3)  # recorded when they were team 3
    book = DossierBook({SWID_A: stale}, owners={9: SWID_A})

    assert book.for_team(9) is not None
    assert book.for_team(3) is None


def test_reseating_refreshes_the_display_fields():
    book = DossierBook({SWID_A: filled(team_id=3, label="dave")})

    moved = book.with_seats({9: SWID_A}, {9: "davey"})

    assert moved.for_team(9).team_id == 9
    assert moved.for_team(9).label == "davey"


def test_a_book_with_no_seating_cannot_answer_by_seat():
    """Honest, rather than guessing off the stale id. Silence beats a wrong name."""
    assert DossierBook({SWID_A: filled()}).for_team(3) is None


# --- answered vs unanswered ------------------------------------------------------


def test_an_empty_dossier_knows_it_is_empty():
    assert seed(SWID_A).is_empty is True
    assert seed(SWID_A).coverage == 0.0


def test_coverage_counts_only_questions_actually_answered():
    dossier = filled()

    assert set(dossier.answered) < set(q.field for q in QUESTIONS)
    assert 0 < dossier.coverage < 1
    assert "seasons_in_league" in dossier.missing


def test_an_empty_string_is_not_an_answer():
    """Otherwise a skipped free-text question would count as coverage."""
    assert BY_FIELD["tells"].is_answered(OwnerDossier(owner_id="X", tells="")) is False
    assert BY_FIELD["tells"].is_answered(OwnerDossier(owner_id="X", tells="hums")) is True


def test_zero_seasons_is_an_answer_even_though_it_is_falsey():
    """A rookie is information. Treating 0 as unanswered would lose it."""
    dossier = OwnerDossier(owner_id="X", seasons_in_league=0)
    assert BY_FIELD["seasons_in_league"].is_answered(dossier) is True


# --- parsing answers -------------------------------------------------------------


def test_a_choice_can_be_typed_in_full():
    assert parse_answer(BY_FIELD["skill"], "sharp") is Skill.SHARP


def test_a_choice_can_be_picked_by_number_because_that_is_how_it_is_shown():
    assert parse_answer(BY_FIELD["skill"], "1") is Skill.SHARP


def test_a_choice_can_be_abbreviated_when_it_is_unambiguous():
    assert parse_answer(BY_FIELD["spend_shape"], "val") is SpendShape.VALUE_HUNTER


def test_a_choice_that_is_not_one_of_the_options_lists_them():
    with pytest.raises(DossierError) as exc:
        parse_answer(BY_FIELD["skill"], "brilliant")
    assert "sharp" in str(exc.value)


def test_positions_are_validated_because_a_typo_would_read_as_a_missing_bias():
    """A silently dropped position reads as "no bias", which is a real answer."""
    assert parse_answer(BY_FIELD["overpays_at"], "qb, rb") == (Position.QB, Position.RB)
    # `Position.parse` is generous — "quarterback" resolves — but a typo must not.
    with pytest.raises(DossierError, match="not a position"):
        parse_answer(BY_FIELD["overpays_at"], "QB, RBB")


def test_a_position_listed_twice_is_recorded_once():
    assert parse_answer(BY_FIELD["ignores"], "TE TE te") == (Position.TE,)


def test_nfl_teams_are_recorded_as_typed_rather_than_validated():
    """Refusing "Niners" because the abbreviation is SF costs more than it buys.

    The point of the field is to capture what you said about a person, and a
    validator that rejects a real answer loses the answer.
    """
    assert parse_answer(BY_FIELD["homer_teams"], "kc, Niners") == ("KC", "NINERS")


def test_a_season_count_has_to_be_a_number():
    assert parse_answer(BY_FIELD["seasons_in_league"], "8") == 8
    with pytest.raises(DossierError):
        parse_answer(BY_FIELD["seasons_in_league"], "ages")


def test_an_unanswered_field_reads_back_as_a_dash():
    assert render_answer(BY_FIELD["skill"], None) == "-"
    assert render_answer(BY_FIELD["homer_teams"], ()) == "-"
    assert render_answer(BY_FIELD["skill"], Skill.CASUAL) == "casual"
    assert render_answer(BY_FIELD["overpays_at"], (Position.QB, Position.TE)) == "QB, TE"


def test_every_question_points_at_a_real_field_and_explains_itself():
    """`QUESTIONS` drives the interview, the form, validation and the readout.

    A question naming a field that does not exist would fail at the far end of a
    twenty-minute interview, which is the worst possible time.
    """
    names = {f for f in OwnerDossier.__dataclass_fields__}
    for question in QUESTIONS:
        assert question.field in names, question.field
        assert question.why.strip(), question.field
        if question.kind == "choice":
            assert question.choices, question.field


# --- the file ---------------------------------------------------------------------


def test_a_dossier_round_trips_through_json(tmp_path: Path):
    path = tmp_path / "dossiers.json"
    save_dossiers(DossierBook({SWID_A: filled()}), path, league_id=99)

    book = load_dossiers(path, owners={3: SWID_A})

    assert book.for_team(3) == filled()


def test_unanswered_fields_are_absent_from_the_file_not_null(tmp_path: Path):
    """An absent key reads as "not asked"; a null reads as "asked and refused".

    They are different states, and the file should say which.
    """
    path = tmp_path / "dossiers.json"
    save_dossiers(DossierBook({SWID_A: seed(SWID_A, team_id=3)}), path)

    entry = json.loads(path.read_text(encoding="utf-8"))["owners"]["OWNER-01"]
    assert "skill" not in entry
    assert entry == {"team_id": 3}


def test_a_missing_file_is_an_empty_book_not_an_error(tmp_path: Path):
    """The whole deterministic layer works without a single dossier."""
    book = load_dossiers(tmp_path / "nope.json", owners={3: SWID_A})

    assert len(book) == 0
    assert book.warnings == ()


def test_unreadable_json_costs_advice_and_not_the_draft(tmp_path: Path):
    path = tmp_path / "dossiers.json"
    path.write_text("{not json", encoding="utf-8")

    book = load_dossiers(path)

    assert len(book) == 0
    assert any("could not read" in w for w in book.warnings)


def test_one_bad_entry_does_not_take_the_rest_of_the_file_with_it(tmp_path: Path):
    """This file is hand-edited by design, so it will be hand-edited badly."""
    path = tmp_path / "dossiers.json"
    path.write_text(
        json.dumps({
            "owners": {
                "OWNER-01": {"skill": "sharp"},
                "OWNER-02": "not an object",
            }
        }),
        encoding="utf-8",
    )

    book = load_dossiers(path, owners={1: "OWNER-01", 2: "OWNER-02"})

    assert book.for_team(1).skill is Skill.SHARP
    assert book.for_team(2) is None
    assert any("OWNER-02" in w for w in book.warnings)


def test_an_unreadable_field_is_dropped_rather_than_failing_the_entry(tmp_path: Path):
    path = tmp_path / "dossiers.json"
    path.write_text(
        json.dumps({"owners": {"OWNER-01": {"skill": "galaxy-brained", "chases": "often"}}}),
        encoding="utf-8",
    )

    dossier = load_dossiers(path, owners={1: "OWNER-01"}).for_team(1)

    assert dossier.skill is None
    assert dossier.chases is Chases.OFTEN


def test_an_entry_with_no_owner_id_is_refused():
    with pytest.raises(DossierError, match="owner_id"):
        from_dict({"skill": "sharp"})


def test_to_dict_and_from_dict_are_inverse_for_a_full_record():
    dossier = filled(seasons_in_league=8, notes="bids up his brother")
    assert from_dict(to_dict(dossier)) == dossier


# --- coverage across the league ----------------------------------------------------


def test_league_coverage_counts_people_we_have_any_read_on():
    seats = {1: SWID_A, 2: SWID_B, 3: "{OWNER-03}"}
    book = DossierBook(
        {SWID_A: filled(SWID_A), SWID_B: seed(SWID_B)}, owners=seats
    )

    # A seeded-but-unanswered entry is not a read.
    assert book.coverage(seats) == pytest.approx(1 / 3)
