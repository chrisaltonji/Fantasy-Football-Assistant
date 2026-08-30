"""`ffa night` — the checklist made executable.

What is worth testing here is not that it can start a draft; `cmd_draft` is
covered next door. It is the two quiet failures the command exists to prevent:

- a dashboard pointed at **last week's run**, because `latest_run` resolves by
  journal mtime and `--new` mints the id inside `cmd_draft`;
- a draft that starts anyway when a gate has failed, which on draft night means
  finding out at pick 40.

Plus the teardown, because an orphan server holding 8765 is what makes the *next*
start fail, at the worst possible moment to discover it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ffa.cli import night


def args(**kw):
    base = dict(
        config=Path("config/league.toml"), runs=Path("runs"),
        dossiers=Path("data/dossiers.json"),
        precedent=Path("data/manager_precedent.json"),
        source="manual", cdp_port=9222, assist=False,
        dashboard_port=8765, watch_port=8766, no_open=True,
        no_dashboard=False, watch=False, launch_chrome=False, check_only=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# --- the gates --------------------------------------------------------------


def test_a_hard_gate_stops_everything_before_a_process_is_spawned(monkeypatch):
    """A draft begun without the reference sheet is worse than one begun two
    minutes later. Nothing is started, and it says so."""
    monkeypatch.setattr(night, "preflight",
                        lambda a, emit=print: [night.Gate("config", False, "nope")])
    spawned = []
    monkeypatch.setattr(night, "_spawn_dashboard",
                        lambda *a, **k: spawned.append(1))

    code = night.cmd_night(args())

    assert code == 2
    assert spawned == []


def test_a_soft_gate_warns_and_the_draft_still_runs(monkeypatch):
    """The tool drafted for months with no dossiers and no plan. Refusing to
    start over a missing nicety would be its own failure."""
    monkeypatch.setattr(night, "preflight", lambda a, emit=print: [
        night.Gate("config", True, "fine"),
        night.Gate("dossiers", False, "none", hard=False),
    ])
    ran = []
    monkeypatch.setattr("ffa.cli.app.cmd_draft", lambda a: ran.append(a) or 0)

    assert night.cmd_night(args(no_dashboard=True)) == 0
    assert len(ran) == 1


def test_check_only_runs_the_gates_and_starts_nothing(monkeypatch):
    """The thing to run at 7:45pm."""
    monkeypatch.setattr(night, "preflight",
                        lambda a, emit=print: [night.Gate("config", True, "fine")])
    ran = []
    monkeypatch.setattr("ffa.cli.app.cmd_draft", lambda a: ran.append(a) or 0)

    assert night.cmd_night(args(check_only=True)) == 0
    assert ran == []


def test_a_gate_reports_its_own_severity_in_the_line():
    assert "[FAIL]" in night.Gate("x", False).line()
    assert "[warn]" in night.Gate("x", False, hard=False).line()
    assert "[ok  ]" in night.Gate("x", True).line()


# --- the browser ------------------------------------------------------------


def test_no_browser_and_no_tab_are_different_answers(monkeypatch):
    """One is fixable by launching Chrome, the other only by a person. Collapsing
    them into a falsy value would have the command offer the wrong remedy."""
    monkeypatch.setattr(night, "_cdp", lambda *a, **k: None)
    assert night.draft_tabs(9222) is None

    monkeypatch.setattr(night, "_cdp", lambda *a, **k: [
        {"type": "page", "url": "https://fantasy.espn.com/football/team?x"}])
    assert night.draft_tabs(9222) == []


def test_only_draft_room_pages_count(monkeypatch):
    monkeypatch.setattr(night, "_cdp", lambda *a, **k: [
        {"type": "page", "url": "https://fantasy.espn.com/football/draft?leagueId=1"},
        {"type": "page", "url": "https://fantasy.espn.com/football/team?teamId=3"},
        {"type": "background_page", "url": "https://fantasy.espn.com/football/draft"},
    ])
    tabs = night.draft_tabs(9222)

    assert len(tabs) == 1
    assert "draft?" in tabs[0]["url"]


def test_two_draft_rooms_is_a_hard_failure(monkeypatch):
    """Attaching to the wrong one succeeds, matches 12/12 and then never moves —
    the worst way for this to fail, so it is refused rather than guessed."""
    monkeypatch.setattr(night, "draft_tabs", lambda port: [{"title": "a"}, {"title": "b"}])
    monkeypatch.setattr("ffa.config.loader.load_config", lambda p: _config())
    monkeypatch.setattr("ffa.cli.app.load_book", lambda c: ["x"] * 300)

    gates = night.preflight(args(source="espn"))
    room = next(g for g in gates if g.name == "draft room")

    assert not room.ok and room.hard
    assert "close all but one" in room.detail


def test_a_manual_draft_is_not_asked_about_a_browser(monkeypatch):
    monkeypatch.setattr("ffa.config.loader.load_config", lambda p: _config())
    monkeypatch.setattr("ffa.cli.app.load_book", lambda c: ["x"] * 300)

    names = {g.name for g in night.preflight(args(source="manual"))}
    assert "draft room" not in names and "debug Chrome" not in names


def _config():
    class C:
        name = "Test League"
        team_count = 12
        budget = 200
        my_team_id = 3
        managers = {3: "christopher"}
        strategy = type("S", (), {"is_set": True})()
    return C()


def test_a_missing_api_key_is_fatal_only_when_the_assistant_was_asked_for(monkeypatch):
    """`--assist` is an explicit request for the thing it pays for. Starting
    without it looks identical to an assistant that had nothing to say."""
    monkeypatch.setattr("ffa.config.loader.load_config", lambda p: _config())
    monkeypatch.setattr("ffa.cli.app.load_book", lambda c: ["x"] * 300)
    monkeypatch.setattr("ffa.config.loader.load_assist_credentials",
                        lambda required=True: (_ for _ in ()).throw(RuntimeError("no key")))

    with_assist = night.preflight(args(assist=True))
    key = next(g for g in with_assist if g.name == "api key")
    assert not key.ok and key.hard

    assert not any(g.name == "api key" for g in night.preflight(args(assist=False)))


# --- the dashboard points at the right night --------------------------------


def test_the_dashboard_waits_for_the_run_the_draft_creates(monkeypatch, tmp_path):
    """**The quiet failure this command exists to prevent.**

    `ffa dashboard` resolves its default run by journal mtime, and `--new` mints
    the id inside `cmd_draft` — so spawning first attaches it to the previous
    draft and spawning after a sleep is a race. It waits for `latest_run` to
    actually change, then passes `--run-dir` explicitly."""
    old, new = tmp_path / "old", tmp_path / "new"
    seen = iter([old, old, new])
    monkeypatch.setattr("ffa.state.recovery.latest_run", lambda runs: next(seen, new))

    got = []
    monkeypatch.setattr(night, "_spawn_dashboard",
                        lambda run_dir, a, emit=print: got.append(run_dir) or object())

    spawned = []
    night.watch_for_run(old, args(runs=tmp_path), spawned, emit=lambda *a: None)

    assert got == [new], "the dashboard attached to the wrong draft"
    assert len(spawned) == 1


def test_it_never_attaches_to_the_previous_draft(monkeypatch, tmp_path):
    """If the run never changes, it says so rather than opening last week's."""
    old = tmp_path / "old"
    monkeypatch.setattr("ffa.state.recovery.latest_run", lambda runs: old)
    monkeypatch.setattr(night, "RUN_WAIT_SECONDS", 0.2)

    got, said = [], []
    monkeypatch.setattr(night, "_spawn_dashboard",
                        lambda *a, **k: got.append(1) or object())

    night.watch_for_run(old, args(runs=tmp_path), [], emit=said.append)

    assert got == []
    assert any("did not create" in s for s in said)


def test_the_dashboard_is_given_the_run_dir_explicitly(monkeypatch, tmp_path):
    """Not left to the default. The default is what resolves to last week."""
    cmds = []

    class P:
        def terminate(self): pass

    monkeypatch.setattr(night.subprocess, "Popen",
                        lambda cmd, **kw: cmds.append(cmd) or P())
    night._spawn_dashboard(tmp_path / "run-x", args(), emit=lambda *a: None)

    assert "--run-dir" in cmds[0]
    assert str(tmp_path / "run-x") in cmds[0]


# --- teardown ---------------------------------------------------------------


def test_the_dashboard_is_stopped_when_the_draft_ends(monkeypatch):
    """An orphan holding 8765 is what makes the *next* start fail."""
    killed = []

    class P:
        def terminate(self): killed.append(1)

    monkeypatch.setattr(night, "preflight",
                        lambda a, emit=print: [night.Gate("config", True)])
    monkeypatch.setattr(night, "_spawn_watch", lambda a, emit=print: P())
    monkeypatch.setattr("ffa.cli.app.cmd_draft", lambda a: 0)

    night.cmd_night(args(watch=True, no_dashboard=True))
    assert killed == [1]


def test_teardown_runs_even_when_the_draft_raises(monkeypatch):
    class P:
        def terminate(self): killed.append(1)

    killed = []
    monkeypatch.setattr(night, "preflight",
                        lambda a, emit=print: [night.Gate("config", True)])
    monkeypatch.setattr(night, "_spawn_watch", lambda a, emit=print: P())
    monkeypatch.setattr("ffa.cli.app.cmd_draft",
                        lambda a: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        night.cmd_night(args(watch=True, no_dashboard=True))
    assert killed == [1]


def test_the_scrape_diagnostic_gets_its_own_port(monkeypatch):
    """It defaults to 8765, the same as the dashboard. Fine when only one runs;
    a collision here."""
    cmds = []

    class P:
        def terminate(self): pass

    monkeypatch.setattr(night.subprocess, "Popen",
                        lambda cmd, **kw: cmds.append(cmd) or P())
    night._spawn_watch(args(), emit=lambda *a: None)

    assert "8766" in cmds[0]
    assert "8765" not in cmds[0]


# --- defaults ---------------------------------------------------------------


def test_night_defaults_to_a_new_espn_draft_with_the_assistant_on():
    """This command exists for one evening; its defaults should be that evening."""
    from ffa.cli.app import build_parser

    parsed = build_parser().parse_args(["night"])

    assert parsed.source == "espn"
    assert parsed.assist is True
    assert parsed.new is True
    assert parsed.func is night.cmd_night


def test_the_budget_default_leaves_headroom_over_the_measured_cost():
    """A full draft measures around $10. A cap that mutes at pick 120 is worse
    than one never reached — and 25 was chosen before anything was measured."""
    from ffa.cli.app import build_parser

    assert build_parser().parse_args(["night"]).assist_budget == 20.0
