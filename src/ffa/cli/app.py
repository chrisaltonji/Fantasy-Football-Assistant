"""Command-line entry point.

Thin on purpose: this dispatches to subcommands and formats errors. All logic
lives in the engine packages so a future web UI can drive the same code.

Checkpoint 1 ships only `config check` and `version` — the draft REPL, data
tools, sim, and replay land in later checkpoints.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ffa.config.loader import DEFAULT_CONFIG_PATH, load_config, load_credentials
from ffa.config.schema import ConfigError

__version__ = "0.1.0"


def cmd_config_check(args: argparse.Namespace) -> int:
    config = load_config(args.path)
    creds = load_credentials(required=config.private)

    print(f"config: {args.path}  OK")
    print(f"  league       {config.name or '(unnamed)'} #{config.league_id} ({config.year})")
    print(f"  visibility   {'private' if config.private else 'public'}")
    print(f"  draft        {config.draft_type}, ${config.budget}/team")
    print(f"  teams        {config.team_count}  (my_team_id={config.my_team_id or 'UNSET'})")
    print(f"  roster       {config.draftable_slots} draftable slots, "
          f"{config.starting_slots} starters")
    print(f"  league money ${config.total_league_money}")
    print(f"  credentials  {'loaded' if creds else 'none (public league)'}")

    if config.private and not creds:  # pragma: no cover - load_credentials raises first
        print("  WARNING: private league with no credentials", file=sys.stderr)
    if not config.my_team_id:
        print(
            "  WARNING: teams.my_team_id is unset — the tool cannot tell which "
            "team is yours, so 'my budget' advice will be wrong.",
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ffa", description="ESPN live auction draft assistant")
    parser.add_argument("--version", action="version", version=f"ffa {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    config_parser = sub.add_parser("config", help="inspect and validate league config")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)

    check = config_sub.add_parser("check", help="validate config/league.toml and credentials")
    check.add_argument("--path", type=Path, default=DEFAULT_CONFIG_PATH)
    check.set_defaults(func=cmd_config_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        # Config problems are user-fixable; a traceback would only obscure them.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
