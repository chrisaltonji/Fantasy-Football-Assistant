"""Draft night, as one command.

`docs/BUILD_STATUS.md` carries an eleven-step ordered checklist and the README
says "two terminals". Both are correct and neither is something to run at 7:58pm
with a draft starting. The two failures they invite are quiet ones: a dashboard
pointed at last week's run because `latest_run` resolves by journal mtime, and an
assistant that never starts because a key did not load — which looks exactly like
an assistant with nothing to say.

So this does three things and refuses to do them out of order.

**It checks before it spawns.** Every gate here is an existing one-shot call;
none of it is new logic. A hard gate failing stops the whole thing before a
single process starts, because a draft begun without the reference sheet is worse
than a draft begun two minutes later.

**It owns the dashboard's lifetime.** Spawned as a separate process — deliberately
separate, so a render bug cannot take the draft down with it — and terminated in a
`finally`, so quitting the draft does not leave a server holding 8765.

**It runs the draft in the foreground, in-process.** The REPL owns stdin and the
console exactly as it does today; nothing here wraps or proxies it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# The one-shot preflight gates come from the modules that already own them, so
# there is one definition of "is the config valid" rather than two that drift.
from ffa.config.schema import ConfigError

# How long to wait for a draft-room tab to appear after launching Chrome. Long
# enough to log into ESPN and click through to the room, short enough that a
# wedged browser does not hold the terminal forever.
TAB_WAIT_SECONDS = 300
TAB_POLL_SECONDS = 2.0

# How long to wait for the draft to mint its run directory before giving up on
# pointing the dashboard at it. The draft creates it within a second of starting;
# this is generous because being wrong here means watching the wrong draft.
RUN_WAIT_SECONDS = 30

DRAFT_URL_FRAGMENT = "fantasy.espn.com/football/draft"


class Gate:
    """One preflight check and what it found."""

    __slots__ = ("name", "ok", "detail", "hard")

    def __init__(self, name: str, ok: bool, detail: str = "", *, hard: bool = True):
        self.name = name
        self.ok = ok
        self.detail = detail
        # A hard gate stops the draft. A soft one prints and carries on — the
        # tool drafted perfectly well without dossiers or a plan for months, and
        # refusing to start over a missing nicety would be its own failure.
        self.hard = hard

    def line(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.hard else "warn")
        return f"  [{mark}] {self.name:<26} {self.detail}"


# --- the browser ------------------------------------------------------------


def _cdp(port: int, path: str, timeout: float = 3.0):
    """One CDP query, or `None`. Never raises — a closed browser is an answer."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def draft_tabs(port: int) -> list[dict] | None:
    """Draft-room tabs open in the debug Chrome, or `None` if it is not running.

    `None` and `[]` are different answers and the caller treats them differently:
    no browser is fixable by launching one, no tab is fixable only by a person.
    """
    listing = _cdp(port, "/json/list")
    if listing is None:
        return None
    return [t for t in listing
            if t.get("type") == "page" and DRAFT_URL_FRAGMENT in t.get("url", "")]


def wait_for_tab(port: int, *, emit=print) -> bool:
    """Poll until exactly one draft room is open, or time out.

    There is an unavoidable human step in the middle of this command — logging
    into ESPN and opening the room — and the honest thing is to wait for it
    rather than fail and make someone re-run everything.
    """
    emit(f"\nwaiting for a draft room in the debug Chrome (up to "
         f"{TAB_WAIT_SECONDS // 60} minutes)")
    emit("  log into ESPN in that window, then open your draft room")

    deadline = time.monotonic() + TAB_WAIT_SECONDS
    said = False
    while time.monotonic() < deadline:
        tabs = draft_tabs(port)
        if tabs:
            if len(tabs) == 1:
                emit(f"  found: {tabs[0].get('title', '')[:60]}")
                return True
            if not said:
                emit(f"  ! {len(tabs)} draft rooms are open — close all but one")
                said = True
        time.sleep(TAB_POLL_SECONDS)

    emit("  gave up waiting.")
    return False


def launch_chrome(port: int, *, emit=print) -> bool:
    """Start the debug Chrome the ESPN reader attaches to.

    Delegates to the probe rather than reimplementing the argv: Chrome refuses
    `--remote-debugging-port` on the default profile, and that constraint plus
    the profile path is already written down in one place.
    """
    root = Path(__file__).resolve().parents[3]
    probe = root / "tools" / "draft_room_probe.py"
    if not probe.is_file():
        emit(f"  ! cannot find {probe}")
        return False

    emit("launching Chrome with a debug port")
    subprocess.Popen([sys.executable, str(probe), "--launch", "--port", str(port)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _cdp(port, "/json/version") is not None:
            return True
        time.sleep(1.0)
    emit("  ! Chrome did not come up on the debug port")
    return False


# --- the gates --------------------------------------------------------------


def preflight(args, *, emit=print) -> list[Gate]:
    """Every cheap check, in the order a failure is worth hearing about."""
    from ffa.cli.app import load_book
    from ffa.config.loader import load_config

    gates: list[Gate] = []
    config = None

    try:
        config = load_config(args.config)
        gates.append(Gate("config", True,
                          f"{config.name} — {config.team_count} teams, "
                          f"${config.budget} each"))
    except (ConfigError, OSError) as exc:
        gates.append(Gate("config", False, str(exc)))
        return gates                       # nothing below this means anything

    if not config.my_team_id:
        gates.append(Gate("your team", False,
                          "teams.my_team_id is unset — every projection keys off it"))
    else:
        label = dict(getattr(config, "managers", {}) or {}).get(config.my_team_id, "")
        gates.append(Gate("your team", True, f"{config.my_team_id} {label}"))

    try:
        book = load_book(config)
        n = len(book) if book else 0
        gates.append(Gate("auction values", bool(n), f"{n} players"
                          if n else "none — run `ffa data fetch`"))
    except Exception as exc:               # noqa: BLE001 - reported, not raised
        gates.append(Gate("auction values", False, str(exc)))

    strategy = getattr(config, "strategy", None)
    declared = bool(strategy and getattr(strategy, "is_set", False))
    gates.append(Gate("draft plan", declared,
                      "declared" if declared
                      else "none — the plan channel stays quiet",
                      hard=False))

    try:
        from ffa.cli.app import _load_dossiers
        dossiers = _load_dossiers(config, args.dossiers)
        filled = len(dossiers) if dossiers else 0
        gates.append(Gate("dossiers", filled > 0,
                          f"{filled} manager(s)" if filled
                          else "none — reads will say 'thin'", hard=False))
    except Exception as exc:               # noqa: BLE001
        gates.append(Gate("dossiers", False, str(exc), hard=False))

    if args.assist:
        try:
            from ffa.config.loader import load_assist_credentials
            load_assist_credentials(required=True)
            gates.append(Gate("api key", True, "loaded"))
        except Exception as exc:           # noqa: BLE001
            # Hard, and deliberately so: `--assist` is an explicit request for
            # the thing it pays for, and starting without it looks identical to
            # an assistant that simply had nothing to say.
            gates.append(Gate("api key", False, str(exc)))

    if args.source == "espn":
        tabs = draft_tabs(args.cdp_port)
        if tabs is None:
            gates.append(Gate("debug Chrome", False,
                              f"nothing on port {args.cdp_port}"))
        elif len(tabs) == 1:
            gates.append(Gate("draft room", True, tabs[0].get("title", "")[:44]))
        elif not tabs:
            gates.append(Gate("draft room", False, "Chrome is up, no draft room open"))
        else:
            # Attaching to the wrong one succeeds, matches 12/12 and then never
            # moves, which is the worst way for this to fail.
            gates.append(Gate("draft room", False,
                              f"{len(tabs)} are open — close all but one"))

    return gates


# --- the dashboard ----------------------------------------------------------


def _spawn_dashboard(run_dir: Path, args, *, emit=print) -> subprocess.Popen | None:
    """Start the dashboard, then check that it is actually up.

    **A `Popen` that returns is not a process that is running**, and the first
    version of this said so anyway. On the first live run it printed its URL and
    the server was never there — with `stderr` going to `DEVNULL` there was
    nothing to read afterwards, so the cause could not be established at all. Two
    changes, and the second is the one that matters:

    - output goes to `dashboard.log` in the run directory, beside the journal it
      is rendering, so a crash leaves evidence;
    - the port is probed before the URL is printed, so the line on screen means
      the thing it says rather than "a process was launched".
    """
    log_path = run_dir / "dashboard.log"
    cmd = [sys.executable, "-m", "ffa.cli.app", "dashboard",
           "--run-dir", str(run_dir), "--port", str(args.dashboard_port),
           "--config", str(args.config)]
    if not args.no_open:
        cmd.append("--open")

    try:
        handle = log_path.open("wb")
    except OSError:
        handle = None                      # a log we cannot write is not fatal

    try:
        process = subprocess.Popen(
            cmd,
            stdout=handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if handle else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:                 # noqa: BLE001
        emit(f"! dashboard did not start: {exc}")
        if handle is not None:
            handle.close()
        return None

    if not _serving(args.dashboard_port):
        emit(f"! the dashboard did not come up on {args.dashboard_port}. "
             f"The draft is unaffected. See {log_path}")
        emit(f"  start it yourself with: ffa dashboard --run-dir {run_dir}")
        return process                     # still returned, so teardown kills it

    emit(f"dashboard  http://127.0.0.1:{args.dashboard_port}  ({run_dir.name})")
    return process


def _serving(port: int, *, timeout: float = 12.0) -> bool:
    """Is something accepting connections on this port yet?

    A plain socket probe rather than an HTTP request: the question is whether the
    server bound, and a page that renders slowly is not a failure.
    """
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        finally:
            sock.close()
        time.sleep(0.5)
    return False


def watch_for_run(before: Path | None, args, spawned: list, *, emit=print) -> None:
    """Point the dashboard at the run the draft is about to create.

    **Watched rather than guessed.** `ffa dashboard` resolves its default run by
    journal mtime, and `--new` mints the id inside `cmd_draft` — so spawning the
    dashboard first attaches it to the previous draft, and spawning it after a
    sleep is a race. This waits for `latest_run` to actually change and then
    passes `--run-dir` explicitly, which cannot attach to the wrong night.
    """
    from ffa.state.recovery import latest_run

    deadline = time.monotonic() + RUN_WAIT_SECONDS
    while time.monotonic() < deadline:
        current = latest_run(args.runs)
        if current is not None and current != before:
            process = _spawn_dashboard(current, args, emit=emit)
            if process is not None:
                spawned.append(process)
            return
        time.sleep(0.5)
    emit("! the draft did not create a run directory; dashboard not started.")


def _spawn_watch(args, *, emit=print) -> subprocess.Popen | None:
    """The raw-DOM-vs-parsed diagnostic, on its own port.

    It defaults to 8765, the same port as the dashboard, which is fine when only
    ever one of them runs and is a collision here. Given a port explicitly.
    """
    root = Path(__file__).resolve().parents[3]
    tool = root / "tools" / "draft_watch.py"
    if not tool.is_file():
        emit(f"! cannot find {tool}")
        return None
    try:
        process = subprocess.Popen(
            [sys.executable, str(tool), "--port", str(args.watch_port),
             "--cdp-port", str(args.cdp_port), "--no-browser"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:                 # noqa: BLE001
        emit(f"! draft_watch did not start: {exc}")
        return None
    emit(f"scrape     http://127.0.0.1:{args.watch_port}")
    return process


# --- the command ------------------------------------------------------------


def cmd_night(args: argparse.Namespace) -> int:
    """Preflight, spawn what watches, then hand the terminal to the draft."""
    from ffa.cli.app import cmd_draft
    from ffa.state.recovery import latest_run

    print("draft night")
    print()

    gates = preflight(args, emit=print)

    # One retry, and only for the gate a person can actually clear right now.
    if args.source == "espn" and args.launch_chrome:
        chrome = next((g for g in gates if g.name in ("debug Chrome", "draft room")), None)
        if chrome is not None and not chrome.ok:
            for line in (g.line() for g in gates):
                print(line)
            if chrome.name == "debug Chrome":
                launch_chrome(args.cdp_port, emit=print)
            if wait_for_tab(args.cdp_port, emit=print):
                gates = preflight(args, emit=print)
            print()

    for gate in gates:
        print(gate.line())
    print()

    failed = [g for g in gates if g.hard and not g.ok]
    if failed:
        print(f"refusing to start: {len(failed)} gate(s) failed.")
        print("nothing was started.")
        return 2

    if args.check_only:
        print("all gates green. Nothing started (--check-only).")
        return 0

    spawned: list[subprocess.Popen] = []
    try:
        if not args.no_dashboard:
            before = latest_run(args.runs)
            # A daemon thread so a dashboard that never starts cannot hold the
            # draft up, and cannot outlive it either.
            threading.Thread(target=watch_for_run,
                             args=(before, args, spawned),
                             daemon=True).start()
        if args.watch:
            process = _spawn_watch(args, emit=print)
            if process is not None:
                spawned.append(process)

        print()
        return cmd_draft(args)
    finally:
        for process in spawned:
            # Terminated, not left running. An orphan on 8765 is the thing that
            # makes the *next* start fail, which is the worst time to find it.
            try:
                process.terminate()
            except Exception:              # noqa: BLE001 - teardown never raises
                pass
        if spawned:
            print("stopped the dashboard.")


def add_parser(sub, *, default_config, default_runs) -> None:
    """Wire `ffa night` up beside `ffa draft`."""
    night = sub.add_parser(
        "night", help="preflight, dashboard and the draft, in one command")

    night.add_argument("--resume", action="store_true",
                       help="resume the most recent draft instead of starting one")
    night.add_argument("--resume-id", metavar="DRAFT_ID", default=None)
    night.add_argument("--source", choices=["manual", "espn"], default="espn",
                       help="where picks come from (default espn — this command "
                            "exists for draft night)")
    night.add_argument("--cdp-port", type=int, default=9222)
    night.add_argument("--tab", default="")
    night.add_argument("--allow-finished-board", action="store_true")
    night.add_argument("--ignore-unmatched-teams", action="store_true")

    night.add_argument("--no-assist", dest="assist", action="store_false",
                       help="run the arithmetic only")
    night.add_argument("--assist-budget", type=float, default=20.0,
                       metavar="DOLLARS",
                       help="hard cap for one draft (default 20; a full draft "
                            "measures around $10)")

    night.add_argument("--no-dashboard", action="store_true")
    night.add_argument("--dashboard-port", type=int, default=8765)
    night.add_argument("--no-open", action="store_true",
                       help="start the dashboard without opening a browser")
    night.add_argument("--watch", action="store_true",
                       help="also serve the raw-scrape diagnostic")
    night.add_argument("--watch-port", type=int, default=8766)

    night.add_argument("--launch-chrome", action="store_true",
                       help="start the debug Chrome if it is not running, then "
                            "wait for you to open the draft room")
    night.add_argument("--check-only", action="store_true",
                       help="run the gates and stop")

    night.add_argument("--dossiers", type=Path, default=Path("data/dossiers.json"))
    night.add_argument("--precedent", type=Path,
                       default=Path("data/manager_precedent.json"))
    night.add_argument("--config", type=Path, default=default_config)
    night.add_argument("--runs", type=Path, default=default_runs)

    # `cmd_draft` reads `--new`; night always means a new draft unless resuming.
    night.set_defaults(func=cmd_night, assist=True, new=True)
