"""`ffa dossier` — the twenty-minute pass that makes bid forecasting worth having.

The interview is the long pole of the whole build and it is done once, under no
time pressure, by a person recalling twelve seasons of other people's habits. So
what these assert is mostly about not wasting that: answers saved as they are
given rather than at the end, a bad answer costing one question rather than the
pass, and the file staying hand-editable afterwards.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from ffa.cli.app import main
from ffa.dossier.schema import QUESTIONS
from ffa.dossier.store import load_dossiers

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "league.example.toml"


@pytest.fixture
def league(tmp_path: Path) -> Path:
    """A three-team league with real owner SWIDs recorded."""
    text = (
        EXAMPLE.read_text(encoding="utf-8")
        .replace("count = 12", "count = 3")
        .replace("ids = []", "ids = [1, 2, 3]")
        .replace("my_team_id = 0", "my_team_id = 1")
    )
    text += '\n[owners]\n1 = "{OWNER-01}"\n2 = "{OWNER-02}"\n3 = "{OWNER-03}"\n'
    path = tmp_path / "league.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def dossiers(tmp_path: Path) -> Path:
    return tmp_path / "dossiers.json"


def run(*args, league=None, dossiers=None) -> int:
    return main(
        ["dossier", *args, "--config", str(league), "--path", str(dossiers)]
    )


def entries(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["owners"]


def as_terminal(monkeypatch, answers):
    replies = list(answers)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt="": replies.pop(0) if replies else ""
    )


# --- init --------------------------------------------------------------------------


def test_init_seeds_one_entry_per_seat_keyed_by_swid(league, dossiers):
    assert run("init", league=league, dossiers=dossiers) == 0

    assert set(entries(dossiers)) == {"OWNER-01", "OWNER-02", "OWNER-03"}


def test_init_refuses_to_flatten_an_existing_file(league, dossiers):
    """Losing an interview to a re-run of the setup command would be brutal."""
    run("init", league=league, dossiers=dossiers)
    dossiers.write_text(
        json.dumps({"owners": {"OWNER-01": {"skill": "sharp"}}}), encoding="utf-8"
    )

    assert run("init", league=league, dossiers=dossiers) == 2
    assert entries(dossiers)["OWNER-01"]["skill"] == "sharp"


def test_init_says_so_when_a_league_records_no_owners(tmp_path, dossiers):
    """Without a SWID there is nothing stable to attach a dossier to."""
    path = tmp_path / "league.toml"
    path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    assert run("init", league=path, dossiers=dossiers) == 2


# --- the form ----------------------------------------------------------------------


def test_the_form_has_a_page_per_manager_and_explains_every_question(
    league, dossiers, tmp_path
):
    """Recall happens on the couch, not at a prompt."""
    run("init", league=league, dossiers=dossiers)
    out = tmp_path / "form.md"

    assert main(["dossier", "form", "--config", str(league), "--path", str(dossiers),
                 "--out", str(out)]) == 0

    text = out.read_text(encoding="utf-8")
    assert text.count("## team ") == 3
    for question in QUESTIONS:
        assert question.prompt in text
        assert question.why in text


def test_the_form_is_prefilled_with_what_is_already_known(league, dossiers, tmp_path):
    """A second pass should be about the gaps, not a retype of the answers."""
    run("init", league=league, dossiers=dossiers)
    data = json.loads(dossiers.read_text(encoding="utf-8"))
    data["owners"]["OWNER-02"]["skill"] = "sharp"
    dossiers.write_text(json.dumps(data), encoding="utf-8")

    out = tmp_path / "form.md"
    main(["dossier", "form", "--config", str(league), "--path", str(dossiers),
          "--out", str(out)])

    page = out.read_text(encoding="utf-8").split("## team 2")[1]
    assert "**skill**: sharp" in page


# --- the interview -----------------------------------------------------------------


def test_the_interview_records_what_you_type(league, dossiers, monkeypatch):
    run("init", league=league, dossiers=dossiers)
    as_terminal(monkeypatch, ["Dave", "8", "sharp"] + [""] * 40)

    assert run("interview", "--team", "2", league=league, dossiers=dossiers) == 0

    dossier = load_dossiers(dossiers, owners={2: "{OWNER-02}"}).for_team(2)
    assert dossier.real_name == "Dave"
    assert dossier.seasons_in_league == 8
    assert dossier.skill.value == "sharp"
    assert dossier.updated_at  # staleness is a real signal; it gets stamped


def test_a_bad_answer_costs_one_question_not_the_pass(league, dossiers, monkeypatch, capsys):
    """You are eleven managers in. Losing the lot to a typo is the wrong trade."""
    run("init", league=league, dossiers=dossiers)
    as_terminal(monkeypatch, ["Dave", "loads", "8", "sharp"] + [""] * 40)

    assert run("interview", "--team", "2", league=league, dossiers=dossiers) == 0

    dossier = load_dossiers(dossiers, owners={2: "{OWNER-02}"}).for_team(2)
    assert dossier.seasons_in_league == 8
    assert "whole number" in capsys.readouterr().err


def test_each_manager_is_saved_before_moving_on(league, dossiers, monkeypatch):
    """A twenty-minute interview must not be lost to a stray Ctrl-C at manager ten."""
    run("init", league=league, dossiers=dossiers)

    replies = ["Chris"] + [""] * (len(QUESTIONS) - 1)
    state = {"n": 0}

    def answer(_prompt=""):
        state["n"] += 1
        if state["n"] > len(replies):
            raise KeyboardInterrupt
        return replies[state["n"] - 1]

    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", answer)

    assert run("interview", league=league, dossiers=dossiers) == 0

    assert load_dossiers(dossiers, owners={1: "{OWNER-01}"}).for_team(1).real_name == "Chris"


def test_a_dash_clears_an_answer(league, dossiers, monkeypatch):
    run("init", league=league, dossiers=dossiers)
    as_terminal(monkeypatch, ["Dave"] + [""] * 40)
    run("interview", "--team", "2", league=league, dossiers=dossiers)

    as_terminal(monkeypatch, ["-"] + [""] * 40)
    run("interview", "--team", "2", league=league, dossiers=dossiers)

    assert load_dossiers(dossiers, owners={2: "{OWNER-02}"}).for_team(2).real_name == ""


def test_a_question_mark_explains_why_it_is_being_asked(league, dossiers, monkeypatch, capsys):
    """The answers get shallow fast unless you know what they are for."""
    run("init", league=league, dossiers=dossiers)
    as_terminal(monkeypatch, ["?", "Dave"] + [""] * 40)

    run("interview", "--team", "2", league=league, dossiers=dossiers)

    assert QUESTIONS[0].why in capsys.readouterr().out


def test_the_interview_can_be_pointed_at_a_manager_by_nickname(league, dossiers, monkeypatch):
    run("init", league=league, dossiers=dossiers)
    as_terminal(monkeypatch, ["Dave"] + [""] * 40)

    # `dave` is team 2 in the shipped example config's [managers] block.
    assert run("interview", "--team", "dave", league=league, dossiers=dossiers) == 0
    assert load_dossiers(dossiers, owners={2: "{OWNER-02}"}).for_team(2).real_name == "Dave"


def test_a_scripted_run_is_told_to_use_the_form_instead(league, dossiers, capsys):
    """No terminal means nowhere to prompt. It must not hang or half-write."""
    run("init", league=league, dossiers=dossiers)

    assert run("interview", league=league, dossiers=dossiers) == 2
    assert "ffa dossier form" in capsys.readouterr().err


# --- reading it back ----------------------------------------------------------------


def test_status_shows_coverage_and_what_is_missing(league, dossiers, capsys, monkeypatch):
    run("init", league=league, dossiers=dossiers)
    as_terminal(monkeypatch, ["Dave"] + [""] * 40)
    run("interview", "--team", "2", league=league, dossiers=dossiers)

    assert run("status", league=league, dossiers=dossiers) == 0

    out = capsys.readouterr().out
    assert "33%" in out
    assert "seasons_in_league" in out


def test_show_reads_one_dossier_back(league, dossiers, capsys, monkeypatch):
    run("init", league=league, dossiers=dossiers)
    as_terminal(monkeypatch, ["Dave"] + [""] * 40)
    run("interview", "--team", "2", league=league, dossiers=dossiers)
    capsys.readouterr()

    assert run("show", "--team", "2", league=league, dossiers=dossiers) == 0
    assert "Dave" in capsys.readouterr().out


def test_status_survives_a_hand_broken_file(league, dossiers, capsys):
    """This file is meant to be opened, so it will get edited badly."""
    run("init", league=league, dossiers=dossiers)
    dossiers.write_text("{ oops", encoding="utf-8")

    assert run("status", league=league, dossiers=dossiers) == 0
    assert "could not read" in capsys.readouterr().err


def test_the_example_config_is_still_valid_toml_after_the_fixture_edits(league):
    tomllib.loads(league.read_text(encoding="utf-8"))


# --- asking only what is worth a person's time -------------------------------------


def test_only_asks_the_named_questions(league, dossiers, monkeypatch):
    """After the derived answers are imported, the fields worth a person's time
    are the two nothing can measure. Walking all thirteen to reach them is 132
    keystrokes across twelve managers, which is how an interview does not get
    finished.
    """
    run("init", league=league, dossiers=dossiers)
    asked: list[str] = []

    def answer(prompt=""):
        asked.append(prompt)
        return ""

    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", answer)

    run("interview", "--only", "tells,notes", league=league, dossiers=dossiers)

    fields = {p.strip().split("[")[0].strip().rstrip(":").strip() for p in asked}
    assert fields == {"tells", "notes"}
    # Three managers, two questions each — not thirty-nine prompts.
    assert len(asked) == 6


def test_an_unknown_question_is_refused_with_the_valid_list(league, dossiers, capsys):
    run("init", league=league, dossiers=dossiers)

    assert run("interview", "--only", "vibes", league=league, dossiers=dossiers) == 2

    err = capsys.readouterr().err
    assert "vibes" in err and "tells" in err


def test_gaps_skips_anybody_already_answered(league, dossiers, monkeypatch, capsys):
    """A second pass should be about what is missing, not a retype."""
    run("init", league=league, dossiers=dossiers)

    as_terminal(monkeypatch, ["goes quiet when broke"] + [""] * 40)
    run("interview", "--only", "tells", "--team", "1", league=league, dossiers=dossiers)

    as_terminal(monkeypatch, ["something else entirely"] + [""] * 40)
    run("interview", "--only", "tells", "--gaps", "--team", "1",
        league=league, dossiers=dossiers)

    kept = load_dossiers(dossiers, owners={1: "{OWNER-01}"}).for_team(1)
    assert kept.tells == "goes quiet when broke"
    assert "nothing left to ask" in capsys.readouterr().out


# --- provenance stays out of the human fields -----------------------------------------


def test_derived_provenance_never_occupies_notes():
    """`notes` and `tells` are the two questions no measurement can touch, and
    they are the reason to run the interview at all. Filling one with generated
    text takes the most valuable field in the file out of play.
    """
    from ffa.dossier.ingest import apply_payload
    from ffa.dossier.store import DossierBook

    seats = {1: "{OWNER-01}"}
    book, report = apply_payload(
        DossierBook({}, owners=seats),
        {"1": {"skill": "sharp", "derived_from": "from four seasons"}},
        seats=seats, labels={}, today="2026-08-18",
    )

    entry = book.for_team(1)
    assert entry.derived_from == "from four seasons"
    assert entry.notes == ""
    assert report.rejected == []


def test_provenance_is_not_counted_as_an_answer():
    """Otherwise coverage would report a manager as known because a script
    described itself."""
    from ffa.dossier.ingest import apply_payload
    from ffa.dossier.store import DossierBook

    seats = {1: "{OWNER-01}"}
    book, _ = apply_payload(
        DossierBook({}, owners=seats),
        {"1": {"derived_from": "from four seasons"}},
        seats=seats, labels={}, today="2026-08-18",
    )

    entry = book.for_team(1)
    assert entry is None or entry.is_empty


def test_provenance_is_not_a_question_anybody_gets_asked():
    from ffa.dossier.schema import BY_FIELD

    assert "derived_from" not in BY_FIELD
