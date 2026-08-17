"""Command-line entry point.

Thin on purpose: this dispatches to subcommands and formats errors. All logic
lives in the engine packages so a future web UI or dashboard can drive the same
code without going through argparse.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from ffa.cli.repl import run_repl
from ffa.config.loader import DEFAULT_CONFIG_PATH, load_config, load_credentials
from ffa.config.schema import ConfigError, LeagueConfig
from ffa.domain.events import DraftInitialized, TeamSeed
from ffa.domain.models import LeagueSnapshot
from ffa.domain.reducers import replay
from ffa.ingest.manual.errors import CommandError
from ffa.reference.loader import ReferenceError, load_reference
from ffa.state.journal import JournalError
from ffa.state.recovery import RUNS_DIR, load_run, make_draft_id, latest_run, run_dir
from ffa.state.store import DraftStore, LockError
from ffa.view.model import build_view

__version__ = "0.2.0"


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
    print(f"  managers     {_manager_summary(config)}")
    print(f"  credentials  {'loaded' if creds else 'none (public league)'}")

    if not config.my_team_id:
        print(
            "  WARNING: teams.my_team_id is unset — the tool cannot tell which "
            "team is yours, so 'my budget' advice will be wrong.",
            file=sys.stderr,
        )
    return 0


SAMPLE_REFERENCE = Path("data/fixtures/sample_rankings.csv")


def load_book(config: LeagueConfig):
    """Load reference data, falling back to the sample fixture.

    Never fatal: a draft with no values still tracks budgets and rosters
    perfectly, it just can't advise. Refusing to start would be the wrong
    trade on draft day.
    """
    from ffa.reference.playerbook import PlayerBook, load_playerbook

    path = Path(config.reference_path) if config.reference_path else None
    if path is None or not path.is_file():
        if path is not None:
            print(f"! reference file not found: {path}", file=sys.stderr)
        path = SAMPLE_REFERENCE

    # Flag the sample by identity, not by how we got here. Pointing
    # [reference].path straight at the fixture must warn just as loudly as
    # falling back to it — the values are invented either way.
    is_sample = _same_file(path, SAMPLE_REFERENCE)

    if not path.is_file():  # pragma: no cover - fixture is committed
        return PlayerBook.empty()

    try:
        return load_playerbook(
            path, teams=config.team_count, budget=config.budget,
            baseline_teams=config.baseline_teams, baseline_budget=config.baseline_budget,
            is_sample=is_sample,
        )
    except ReferenceError as exc:
        print(f"! could not load reference data: {exc}", file=sys.stderr)
        return PlayerBook.empty()


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False


def cmd_data_validate(args: argparse.Namespace) -> int:
    """Check an export before draft day, not during it."""
    report = load_reference(args.path)
    print(report.summary())
    return 0 if report.rows else 1


def cmd_draft(args: argparse.Namespace) -> int:
    config = load_config(args.config)

    if args.resume or args.resume_id:
        directory = _resolve_resume_dir(args, config)
        print(f"resuming {directory}")
        store = DraftStore.resume(directory)
    else:
        # Every projection keys off this, so an unset value would silently
        # produce advice about the wrong team. CP1's `config check` only warns;
        # starting a real draft has to be stricter.
        if not config.my_team_id:
            raise ConfigError(
                "teams.my_team_id is not set in your config, so the tool cannot "
                "tell which team is yours. Set it before starting a draft."
            )
        draft_id = make_draft_id(config.league_id, config.year)
        directory = run_dir(draft_id, args.runs)
        print(f"new draft {draft_id}")
        store = DraftStore.create(directory, _init_event(config, draft_id))

    try:
        return run_repl(store, stdin=sys.stdin, stdout=sys.stdout, book=load_book(config))
    finally:
        store.close()


def cmd_sim(args: argparse.Namespace) -> int:
    """Run a simulated auction into a real journal.

    This is the acceptance test the build has been missing: bots bid against
    the same projections the advisory layer reads, every event goes through
    the same store, and the result is a journal that resumes and replays like
    any other. Needs no ESPN access, which is why it can run today.
    """
    from ffa.sim.engine import AuctionSim
    from ffa.sim.faults import FaultProfile
    from ffa.sim.source import SimSource

    config = load_config(args.config)
    if not config.my_team_id:
        raise ConfigError(
            "teams.my_team_id is not set in your config, so the tool cannot "
            "tell which team is yours. Set it before simulating a draft."
        )

    draft_id = f"sim-{args.seed}-{make_draft_id(config.league_id, config.year)}"
    directory = run_dir(draft_id, args.runs)
    if directory.exists() and not args.force:
        raise ConfigError(
            f"{directory} already exists. Use --force to overwrite, or a "
            "different --seed."
        )
    if directory.exists():
        shutil.rmtree(directory)

    try:
        profile = FaultProfile(
            drop_prices=args.drop_prices,
            drop_positions=args.drop_positions,
            late_prices=args.late_prices,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from None

    init = _init_event(config, draft_id)
    sim = AuctionSim(
        league=init,
        book=load_book(config),
        seed=args.seed,
        faults=profile,
        human_team=config.my_team_id if args.against_me else None,
    )
    source = SimSource(sim)

    print(f"simulating {draft_id}")
    print(f"  seed     {args.seed}")
    print(f"  faults   {profile.describe()}")
    print(f"  seat     {'you bid manually' if args.against_me else 'bots fill every seat'}")

    store = DraftStore.create(directory, init)
    try:
        for event in source.events():
            store.dispatch(event)
        state = store.state
    finally:
        store.close()

    print(f"\n{source.status().detail}")
    print(f"journal  {directory / 'events.jsonl'}")
    _print_sim_summary(state, sim)
    return 0


def _print_sim_summary(state, sim) -> int:
    """Per-team outcome, plus the invariants a bad run would violate."""
    from ffa.domain import projections as proj

    league = state.league
    if league is None:  # pragma: no cover - store always applies the init event
        return 0

    print(f"\n{'team':>6}  {'roster':>6}  {'spent':>6}  {'left':>5}  {'unknown':>7}")
    overspent = []
    for team_id in sorted(state.teams):
        spent = proj.spent(state, team_id)
        left = proj.remaining_budget(state, team_id)
        unknown_n = proj.unknown_price_count(state, team_id)
        print(f"{team_id:>6}  {proj.roster_count(state, team_id):>6}  "
              f"${spent:>5}  ${left:>4}  {unknown_n:>7}")
        if left < 0:
            overspent.append(team_id)

    totals = proj.league_totals(state)
    print(f"\n{totals['sales']} sale(s), ${totals['dollars_spent']} spent, "
          f"{totals['unknown_prices']} price(s) still unknown")
    print(f"faults: {sim._injector.summary()}")

    if overspent:
        print(f"! teams over budget: {overspent}", file=sys.stderr)
    for warning in state.warnings[:5]:
        print(f"! {warning}", file=sys.stderr)
    return 0


def cmd_export_state(args: argparse.Namespace) -> int:
    """Emit the JSON view model every surface reads.

    Also the sample-data generator for the dashboard design effort: point it at
    any run directory and you get a real, correctly-shaped payload.
    """
    directory = args.run_dir or latest_run(args.runs)
    if directory is None:
        raise ConfigError(f"no runs found under {args.runs}. Start one with `ffa draft --new`.")

    events, warnings = load_run(directory)
    for warning in warnings:
        print(f"! {warning}", file=sys.stderr)

    book = load_book(load_config(args.config)) if args.config.is_file() else None
    view = build_view(replay(events, directory.name), book)
    text = json.dumps(view, indent=2)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


# --- helpers ------------------------------------------------------------------


def _init_event(config: LeagueConfig, draft_id: str) -> DraftInitialized:
    snapshot = LeagueSnapshot(
        league_id=config.league_id,
        year=config.year,
        name=config.name,
        draft_type=config.draft_type,
        budget=config.budget,
        team_count=config.team_count,
        my_team_id=config.my_team_id,
        roster=dict(config.roster),
        flex_positions=tuple(config.flex_positions),
    )
    teams = tuple(
        TeamSeed(
            team_id=team_id,
            owner_id=config.owners.get(team_id, ""),
            manager=config.managers.get(team_id, ""),
            # Recorded for reference only. Nothing keys off it — team names
            # change too often to be identity.
            name="",
        )
        # Never range(1, count+1): ESPN team ids skip holes, and inventing a
        # phantom team while dropping a real one is silent and catastrophic.
        for team_id in config.effective_team_ids
    )
    return DraftInitialized(draft_id=draft_id, league=snapshot, teams=teams,
                            app_version=__version__)


def _resolve_resume_dir(args: argparse.Namespace, config: LeagueConfig) -> Path:
    if args.resume_id:
        directory = run_dir(args.resume_id, args.runs)
        if not directory.is_dir():
            raise ConfigError(f"no run directory at {directory}")
        return directory

    directory = latest_run(args.runs, league_id=config.league_id, year=config.year)
    if directory is None:
        raise ConfigError(
            f"no previous run for league {config.league_id} under {args.runs}. "
            "Start one with `ffa draft --new`."
        )
    return directory


def _manager_summary(config: LeagueConfig) -> str:
    if not config.managers:
        return "none set (teams addressable as t1..tN)"
    return ", ".join(f"{tid}:{name}" for tid, name in sorted(config.managers.items()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ffa", description="ESPN live auction draft assistant")
    parser.add_argument("--version", action="version", version=f"ffa {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    config_parser = sub.add_parser("config", help="inspect and validate league config")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="validate config/league.toml and credentials")
    check.add_argument("--path", type=Path, default=DEFAULT_CONFIG_PATH)
    check.set_defaults(func=cmd_config_check)

    draft = sub.add_parser("draft", help="run a live draft session")
    draft.add_argument("--new", action="store_true", help="start a new draft")
    draft.add_argument("--resume", action="store_true", help="resume the most recent draft")
    draft.add_argument("--resume-id", metavar="DRAFT_ID", help="resume a specific draft")
    draft.add_argument("--source", choices=["manual"], default="manual",
                       help="where picks come from (espn arrives in CP5)")
    draft.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    draft.add_argument("--runs", type=Path, default=RUNS_DIR)
    draft.set_defaults(func=cmd_draft)

    sim = sub.add_parser("sim", help="run a simulated auction (no ESPN needed)")
    sim.add_argument("--seed", type=int, default=0, help="reproduces a run exactly")
    sim.add_argument("--drop-prices", type=float, default=0.0, metavar="RATE",
                     help="fraction of sales that arrive with no price (0-1)")
    sim.add_argument("--drop-positions", type=float, default=0.0, metavar="RATE",
                     help="fraction of sales that arrive with no position (0-1)")
    sim.add_argument("--late-prices", type=float, default=1.0, metavar="RATE",
                     help="fraction of dropped prices that arrive later (0-1)")
    sim.add_argument("--against-me", action="store_true",
                     help="leave your seat empty instead of filling it with a bot")
    sim.add_argument("--force", action="store_true", help="overwrite an existing run")
    sim.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    sim.add_argument("--runs", type=Path, default=RUNS_DIR)
    sim.set_defaults(func=cmd_sim)

    data = sub.add_parser("data", help="inspect reference data")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    validate = data_sub.add_parser("validate", help="check a rankings/auction-value export")
    validate.add_argument("path", type=Path)
    validate.set_defaults(func=cmd_data_validate)

    export = sub.add_parser("export-state", help="write the JSON view model for a run")
    export.add_argument("--run-dir", type=Path, default=None)
    export.add_argument("--runs", type=Path, default=RUNS_DIR)
    export.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    export.add_argument("--out", type=Path, default=None)
    export.set_defaults(func=cmd_export_state)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, CommandError, JournalError, LockError, ReferenceError) as exc:
        # All user-fixable; a traceback would only bury the message.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
