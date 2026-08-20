"""`ffa dashboard` — the board on a screen, beside the terminal you type into.

Deliberately a second process rather than a mode of `ffa draft`. Two reasons,
and both are about the draft surviving the dashboard rather than the other way
round:

- **The engine keeps one writer.** This process never opens a `DraftStore` and
  never takes the pid lock, so it cannot corrupt journal ordering no matter what
  it does. It is a reader of the same file, and the journal's tolerance for a
  torn trailing line — written for crash-resume — is exactly what makes reading
  a file under active append safe.
- **A crash here costs you a browser tab.** If the dashboard were inside the
  draft loop, a rendering bug at pick 90 would take the thing recording your
  draft down with it. Separate processes mean the worst case is that you go back
  to reading the terminal, which is the surface that was rehearsed anyway.

It follows the *latest* run by default, so the usual sequence is two terminals:
`ffa draft --new --source espn` in one, `ffa dashboard` in the other.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ffa.config.loader import DEFAULT_CONFIG_PATH, load_config
from ffa.config.schema import ConfigError

RUNS_DIR = Path("runs")


def cmd_dashboard(args: argparse.Namespace) -> int:
    from ffa.cli.app import _load_dossiers, _load_precedent, load_book
    from ffa.config.identity import seats
    from ffa.dashboard.server import StateReader, serve
    from ffa.state.recovery import latest_run

    config = load_config(args.config) if args.config.is_file() else None

    run_dir = args.run_dir or latest_run(args.runs)
    if run_dir is None:
        raise ConfigError(
            f"no runs found under {args.runs}. Start one with `ffa draft --new`, "
            "then run this in a second terminal."
        )

    reader = StateReader(
        run_dir,
        config=config,
        book=load_book(config) if config is not None else None,
        dossiers=_load_dossiers(config, args.dossiers),
        precedent=_load_precedent(config, args.precedent) if config is not None else None,
        seats=seats(config) if config is not None else None,
    )

    try:
        server = serve(reader, host=args.host, port=args.port, interval=args.interval)
    except OSError as exc:
        raise ConfigError(
            f"could not bind {args.host}:{args.port} — {exc}. Another dashboard "
            "may already be running; pass --port to use a different one."
        ) from exc

    url = f"http://{args.host}:{args.port}/"
    print(f"dashboard  {url}")
    print(f"  watching  {run_dir}")
    print(f"  polling   every {args.interval:g}s")
    print("  read-only — nothing here can change the draft. Ctrl-C to stop.")

    if not reader.journal.is_file():
        print(f"! no journal at {reader.journal} yet — the page will fill in "
              "once the draft starts.", file=sys.stderr)

    if args.open_it:  # pragma: no cover - interactive
        import webbrowser

        webbrowser.open(url)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped. the draft is untouched.")
    finally:
        server.shutdown()
    return 0


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "dashboard", help="serve the live draft board in a browser (read-only)"
    )
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="which draft to watch (default: the latest run)")
    parser.add_argument("--runs", type=Path, default=RUNS_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--dossiers", type=Path, default=Path("data/dossiers.json"))
    parser.add_argument("--precedent", type=Path,
                        default=Path("data/manager_precedent.json"))
    # Loopback by default and on purpose: this serves every manager's budget and
    # roster over HTTP with no auth. That is fine for the machine you are
    # drafting on and wrong for the network you are drafting from.
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between polls (default: 2)")
    parser.add_argument("--open", dest="open_it", action="store_true",
                        help="open a browser window")
    parser.set_defaults(func=cmd_dashboard)
