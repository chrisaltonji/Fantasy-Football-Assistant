"""Doing the interview somewhere other than a terminal, and getting it back.

Thirteen questions across twelve managers is 156 prompts, and the content is
recall — arguing with yourself about what somebody did in the third round two
years ago. That goes better spoken than typed, so `brief` renders the whole
interview as a prompt and `import` takes the answers back.

The load-bearing property is that `import` is not a second, looser way into the
record. Every value goes through the same `parse_answer` the terminal interview
uses, and anything that does not survive it is dropped and named rather than
quietly rounded into something plausible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffa.dossier.brief import render_brief
from ffa.dossier.ingest import apply_payload, extract_payload
from ffa.dossier.schema import QUESTIONS, DossierError, Pace, Skill, seed
from ffa.dossier.store import DossierBook

SEATS = {1: "{OWNER-01}", 2: "{OWNER-02}", 3: "{OWNER-03}"}
LABELS = {1: "chris", 2: "dave", 3: "sam"}


def book_with(*dossiers) -> DossierBook:
    return DossierBook({d.owner_id: d for d in dossiers}, owners=SEATS)


def apply(payload, book=None):
    return apply_payload(
        book or DossierBook({}, owners=SEATS),
        payload,
        seats=SEATS,
        labels=LABELS,
        today="2026-08-18",
    )


# --- the brief ---------------------------------------------------------------------


def test_the_brief_carries_every_question_with_its_reason():
    """The answers go shallow fast unless whoever is asking knows what they are for."""
    text = render_brief(DossierBook({}, owners=SEATS), seats=SEATS, labels=LABELS)

    for question in QUESTIONS:
        assert question.prompt in text
        assert question.why in text


def test_the_brief_lists_the_exact_keys_a_choice_will_accept():
    """Import validates strictly, so the prompt has to say what will pass."""
    text = render_brief(DossierBook({}, owners=SEATS), seats=SEATS, labels=LABELS)

    for question in QUESTIONS:
        for choice in question.choices:
            assert f"`{choice.key}`" in text


def test_the_brief_tells_the_far_end_not_to_invent_answers():
    """The one instruction that matters.

    An empty dossier is honest and costs nothing. An invented one gets read as
    observation by a layer whose entire job is to trust it.
    """
    text = render_brief(DossierBook({}, owners=SEATS), seats=SEATS, labels=LABELS)

    assert "Never invent an answer" in text
    assert "Omit any field I did not actually answer" in text


def test_the_brief_is_keyed_by_team_id_not_swid():
    """A SWID is opaque and easy to garble in a transcript.

    One wrong character would file a read against the wrong person, silently.
    """
    text = render_brief(DossierBook({}, owners=SEATS), seats=SEATS, labels=LABELS)

    assert "keyed by **team id**" in text
    assert "OWNER-01" not in text


def test_the_brief_shows_what_is_already_recorded_so_a_second_pass_is_about_gaps():
    known = seed(SEATS[2], team_id=2, label="dave")
    known = known.__class__(**{**known.__dict__, "skill": Skill.SHARP})

    text = render_brief(book_with(known), seats=SEATS, labels=LABELS)

    assert "skill=sharp" in text
    assert "nothing yet" in text  # the other two


# --- pulling JSON out of whatever was pasted ----------------------------------------


def test_a_plain_json_file_is_read_directly():
    assert extract_payload('{"3": {"real_name": "Sam"}}') == {"3": {"real_name": "Sam"}}


def test_a_fenced_block_inside_a_transcript_is_found():
    """What a chat actually hands you is prose with a code block in it."""
    payload = extract_payload(
        'Great, that covers Dave.\n\n```json\n{"2": {"skill": "sharp"}}\n```\n\nWant to do Sam?'
    )
    assert payload == {"2": {"skill": "sharp"}}


def test_the_last_block_wins_because_a_correction_comes_after_a_draft():
    text = (
        '```json\n{"2": {"skill": "casual"}}\n```\n'
        "Actually no, he is sharp.\n"
        '```json\n{"2": {"skill": "sharp"}}\n```\n'
    )
    assert extract_payload(text) == {"2": {"skill": "sharp"}}


def test_the_on_disk_shape_can_be_fed_straight_back_in():
    """So a hand-edited dossiers.json is a valid import, not a special case."""
    payload = extract_payload(json.dumps({"owners": {"OWNER-02": {"skill": "sharp"}}}))
    assert payload == {"OWNER-02": {"skill": "sharp"}}


def test_a_file_with_no_json_says_so_rather_than_half_importing():
    with pytest.raises(DossierError, match="found no JSON"):
        extract_payload("we talked about dave, he is sharp")


def test_an_empty_file_is_refused():
    with pytest.raises(DossierError, match="empty"):
        extract_payload("   ")


def test_a_malformed_block_names_the_parse_error():
    with pytest.raises(DossierError, match="does not parse"):
        extract_payload('```json\n{"2": {"skill": }\n```')


# --- merging -----------------------------------------------------------------------


def test_answers_land_against_the_right_person():
    book, report = apply({"2": {"real_name": "Dave", "skill": "sharp"}})

    dossier = book.for_team(2)
    assert dossier.real_name == "Dave"
    assert dossier.skill is Skill.SHARP
    assert report.changed_teams == (2,)


def test_a_swid_key_also_resolves_for_anyone_who_pastes_one():
    book, _ = apply({"{owner-03}": {"real_name": "Sam"}})
    assert book.for_team(3).real_name == "Sam"


def test_lists_are_accepted_because_that_is_how_json_says_it():
    book, _ = apply({"2": {"overpays_at": ["QB", "RB"], "homer_teams": ["kc"]}})

    dossier = book.for_team(2)
    assert [p.value for p in dossier.overpays_at] == ["QB", "RB"]
    assert dossier.homer_teams == ("KC",)


def test_a_field_that_was_not_discussed_is_left_exactly_as_it_was():
    """An import carries what was talked about, not the whole record.

    Treating absence as a clear would make every partial pass destructive, and
    partial passes are the normal case.
    """
    first, _ = apply({"2": {"real_name": "Dave", "skill": "sharp"}})
    second, _ = apply({"2": {"pace": "waits"}}, first)

    dossier = second.for_team(2)
    assert dossier.pace is Pace.WAITS
    assert dossier.real_name == "Dave"
    assert dossier.skill is Skill.SHARP


@pytest.mark.parametrize("blank", ["", "-", "unknown", "n/a", None, "null", []])
def test_a_helpfully_filled_in_blank_is_treated_as_unanswered(blank):
    """Something at the far end will eventually write "unknown" to be tidy.

    Storing that would turn a gap into a finding, and coverage would start
    counting questions nobody answered.
    """
    book, report = apply({"2": {"real_name": "Dave", "tells": blank}})

    assert book.for_team(2).tells == ""
    assert report.applied[2] == ["real_name"]


def test_a_value_that_is_not_one_of_the_choices_is_dropped_and_named():
    """Never coerced. Rounding "hoards everything" to a category would put a
    guess into the record wearing an observation's clothes."""
    book, report = apply({"2": {"skill": "sharp", "spend_shape": "hoards everything"}})

    assert book.for_team(2).spend_shape is None
    assert book.for_team(2).skill is Skill.SHARP
    assert any("spend_shape" in r for r in report.rejected)


def test_a_field_that_does_not_exist_is_reported_not_silently_swallowed():
    book, report = apply({"2": {"vibe": "chill", "real_name": "Dave"}})

    assert book.for_team(2).real_name == "Dave"
    assert any("'vibe'" in r for r in report.rejected)


def test_display_fields_ride_along_without_complaint():
    """`team_id` and `label` appear in the on-disk shape and the config owns them."""
    _, report = apply({"2": {"team_id": 2, "label": "dave", "real_name": "Dave"}})

    assert report.rejected == []


def test_a_team_this_league_does_not_have_is_refused_with_the_real_ids():
    """ESPN ids have holes, so the message has to list them rather than a range."""
    book, report = apply({"99": {"real_name": "Nobody"}})

    assert not report
    assert any("99" in r and "1, 2, 3" in r for r in report.rejected)


def test_an_entry_that_is_not_an_object_is_refused():
    _, report = apply({"2": "sharp"})
    assert any("expected an object" in r for r in report.rejected)


def test_an_import_stamps_the_date_because_staleness_is_a_real_signal():
    book, _ = apply({"2": {"real_name": "Dave"}})
    assert book.for_team(2).updated_at == "2026-08-18"


def test_a_manager_with_nothing_valid_is_not_touched_at_all():
    """No date stamp, no empty entry — an all-rejected import changes nothing."""
    book, report = apply({"2": {"skill": "brilliant"}})

    assert book.for_team(2) is None
    assert not report


def test_the_report_counts_what_actually_landed():
    _, report = apply({
        "2": {"real_name": "Dave", "skill": "sharp"},
        "3": {"pace": "waits"},
    })

    assert report.field_count == 3
    assert report.changed_teams == (2, 3)


# --- the whole loop -----------------------------------------------------------------


def test_a_brief_and_an_import_meet_in_the_middle(tmp_path: Path):
    """The contract the brief publishes has to be the one the importer accepts.

    They are written in different files and drift is invisible until the far end
    hands back an answer nothing will take.
    """
    brief = render_brief(DossierBook({}, owners=SEATS), seats=SEATS, labels=LABELS)

    # The example block in the brief, imported verbatim.
    example = extract_payload(brief)
    book, report = apply(example)

    assert report.rejected == []
    assert report.field_count == 12
    assert book.for_team(3).skill is Skill.SHARP
    # And the example obeys its own rule: a question that was not answered is
    # absent, not present-and-empty.
    assert "avoids_teams" not in example["3"]
    assert book.for_team(3).avoids_teams == ()
