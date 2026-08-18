"""`ffa config nicknames` — the one-off pass that makes corrections typeable.

The command exists because of a specific draft-day sequence: a pick is credited
to the wrong team, you have seconds to fix it, and the only handle the tool
gives you is `espn78078705`. Everything asserted here is about that moment —
the names get written, they get validated before they are written, and nothing
is silently lost.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ffa.cli.app import main
from ffa.config.loader import load_config

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "league.example.toml"


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """A league whose managers are still ESPN's generated usernames."""
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        '[managers]\n1 = "chris"\n2 = "dave"\n3 = "sam"',
        '[managers]\n1 = "espn111"\n2 = "espn222"\n3 = "sam"',
    )
    path = tmp_path / "league.toml"
    path.write_text(text, encoding="utf-8")
    return path


def managers_in(path: Path) -> dict[str, str]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["managers"]


def test_set_writes_a_typeable_name(config_path, capsys):
    assert main(["config", "nicknames", "--path", str(config_path),
                 "--set", "1=chris"]) == 0
    assert managers_in(config_path)["1"] == "chris"


def test_several_sets_land_in_one_pass(config_path):
    main(["config", "nicknames", "--path", str(config_path),
          "--set", "1=chris", "--set", "2=dave"])
    assert managers_in(config_path) == {"1": "chris", "2": "dave", "3": "sam"}


def test_list_shows_the_roster_and_changes_nothing(config_path, capsys):
    before = config_path.read_text(encoding="utf-8")

    assert main(["config", "nicknames", "--path", str(config_path), "--list"]) == 0

    assert config_path.read_text(encoding="utf-8") == before
    assert "ESPN username" in capsys.readouterr().out


def test_the_teams_still_carrying_a_username_are_called_out(config_path, capsys):
    main(["config", "nicknames", "--path", str(config_path), "--list"])
    assert "team 1" in capsys.readouterr().err


def test_a_name_that_would_never_resolve_is_refused_before_it_is_written(config_path):
    """Validation has to happen at write time. A config that only fails on load
    turns one bad keystroke into a tool that will not start."""
    before = config_path.read_text(encoding="utf-8")

    assert main(["config", "nicknames", "--path", str(config_path),
                 "--set", "1=dave smith"]) == 2

    assert config_path.read_text(encoding="utf-8") == before


def test_a_duplicate_nickname_is_refused_because_it_could_mean_either_team(config_path):
    assert main(["config", "nicknames", "--path", str(config_path),
                 "--set", "1=sam"]) == 2
    assert managers_in(config_path)["1"] == "espn111"


def test_a_team_id_this_league_does_not_have_is_refused_with_the_real_ids(
    config_path, capsys
):
    """ESPN ids have holes, so `1-12` would be a misleading thing to print."""
    assert main(["config", "nicknames", "--path", str(config_path),
                 "--set", "99=dave"]) == 2
    assert "not in this league" in capsys.readouterr().err


def test_the_written_config_still_loads(config_path):
    """The round trip is the point: this file is read at the start of a draft."""
    main(["config", "nicknames", "--path", str(config_path),
          "--set", "1=chris", "--set", "2=dave"])

    config = load_config(config_path)
    assert config.managers[1] == "chris"


def test_a_nickname_makes_the_team_resolvable_by_name(config_path, init_event):
    """The whole reason the command exists, asserted end to end.

    Before: the only handle on team 1 is `espn111`. After: `chris`, and the
    prefix `chr` too.
    """
    from dataclasses import replace

    from ffa.domain.models import DraftState
    from ffa.domain.reducers import replay
    from ffa.ingest.manual.resolve import resolve_team

    main(["config", "nicknames", "--path", str(config_path), "--set", "1=chris"])
    config = load_config(config_path)

    seeds = tuple(
        replace(seed, manager=config.managers.get(seed.team_id, ""))
        for seed in init_event.teams
    )
    state: DraftState = replay([replace(init_event, teams=seeds)], init_event.draft_id)

    assert resolve_team(state, "chris") == 1
    assert resolve_team(state, "chr") == 1


# --- the interactive walk ---------------------------------------------------------


def as_terminal(monkeypatch, answers):
    """Pretend stdin is a terminal and answer each prompt in order."""
    replies = list(answers)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt="": replies.pop(0) if replies else ""
    )


def test_walking_the_roster_names_every_team(config_path, monkeypatch):
    as_terminal(monkeypatch, ["chris", "dave"] + [""] * 20)

    assert main(["config", "nicknames", "--path", str(config_path)]) == 0
    assert managers_in(config_path)["1"] == "chris"
    assert managers_in(config_path)["2"] == "dave"


def test_pressing_enter_keeps_what_is_already_there(config_path, monkeypatch):
    """A re-run to fix one name must not cost you the other eleven."""
    as_terminal(monkeypatch, [""] * 24)

    main(["config", "nicknames", "--path", str(config_path)])

    assert managers_in(config_path)["3"] == "sam"


def test_a_dash_clears_a_nickname_back_to_the_slot_form(config_path, monkeypatch):
    as_terminal(monkeypatch, ["", "", "-"] + [""] * 20)

    main(["config", "nicknames", "--path", str(config_path)])

    assert "3" not in managers_in(config_path)


def test_a_bad_answer_is_rejected_in_place_rather_than_ending_the_pass(
    config_path, monkeypatch, capsys
):
    """You are twelve prompts into a chore. Losing the lot over one typo is
    the wrong trade, so the bad answer is refused and the walk continues."""
    as_terminal(monkeypatch, ["dave smith", "dave"] + [""] * 20)

    assert main(["config", "nicknames", "--path", str(config_path)]) == 0
    assert managers_in(config_path)["1"] == "espn111"  # refused, not written
    assert managers_in(config_path)["2"] == "dave"     # the walk carried on
    assert "space" in capsys.readouterr().err


def test_no_arguments_and_no_terminal_says_what_to_do_instead(config_path, capsys):
    """A scripted run has nowhere to prompt, so it must not hang or half-write."""
    assert main(["config", "nicknames", "--path", str(config_path)]) == 2
    assert "--set 3=dave" in capsys.readouterr().err
