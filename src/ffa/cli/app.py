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
from ffa.config.identity import seats
from ffa.config.loader import DEFAULT_CONFIG_PATH, load_config, load_credentials
from ffa.config.schema import ConfigError, LeagueConfig
from ffa.domain.events import DraftInitialized, TeamSeed
from ffa.dossier.schema import DossierError
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
    print(f"  nominations  {_nomination_summary(config)}")
    print(f"  plan         {_strategy_summary(config)}")
    print(f"  credentials  {'loaded' if creds else 'none (public league)'}")
    _print_roster(config)

    if not config.nomination_order:
        # Not fatal, and deliberately not a WARNING on stderr: everything else
        # still works. But `config check` is the last gate before draft day, and
        # a config written before this existed carries no order at all — which is
        # invisible until `turns` says it cannot name a seat.
        print(
            "  note: no nomination order on file, so `turns` cannot say whose "
            "turn it is. `ffa config init --force` re-reads it from ESPN."
        )

    if not config.my_team_id:
        print(
            "  WARNING: teams.my_team_id is unset — the tool cannot tell which "
            "team is yours, so 'my budget' advice will be wrong.",
            file=sys.stderr,
        )

    from ffa.config.nicknames import looks_generated

    untypeable = [
        t for t in config.effective_team_ids
        if t not in config.managers or looks_generated(config.managers[t])
    ]
    if untypeable:
        print(
            "  WARNING: no typeable nickname for team(s) "
            + ", ".join(str(t) for t in untypeable)
            + " — correcting one of their picks would mean typing an ESPN "
            "username under the clock. Fix once with:  ffa config nicknames",
            file=sys.stderr,
        )
    return 0


def cmd_config_init(args: argparse.Namespace) -> int:
    """Rebuild config/league.toml from ESPN.

    `config/league.toml` is gitignored — it carries the real league id, member
    SWIDs and manager nicknames — so it is *expected* to be absent on a new
    machine. Losing it should cost one command, not an afternoon of
    hand-editing against a fixture.
    """
    from ffa.config.carry import carried
    from ffa.config.loader import load_dotenv, write_config
    from ffa.config.nicknames import carry_forward, looks_generated
    from ffa.ingest.espn.settings import config_from_payloads, fetch_league_payloads

    env = load_dotenv()
    league_id = args.league_id or int(env.get("ESPN_LEAGUE_ID") or 0)
    year = args.year or int(env.get("ESPN_SEASON_YEAR") or 0)
    if not league_id or not year:
        raise ConfigError(
            "need --league-id and --year (or ESPN_LEAGUE_ID / ESPN_SEASON_YEAR "
            "in .env)"
        )

    if args.path.exists() and not args.force:
        raise ConfigError(
            f"{args.path} already exists. Use --force to overwrite it."
        )

    creds = None if args.public else load_credentials()
    print(f"reading league {league_id} ({year}) from ESPN")
    settings_payload, teams_payload = fetch_league_payloads(
        league_id, year, cookies=creds.as_cookies() if creds else None
    )

    config, warnings = config_from_payloads(
        settings_payload, teams_payload,
        league_id=league_id, year=year,
        swid=creds.swid if creds else None,
        private=not args.public,
    )

    # Every name ESPN itself offers for each seat. Anything in here was supplied
    # rather than chosen, so it must never be preserved as if you had typed it.
    from ffa.ingest.espn.settings import teams_from_payload

    directory = teams_from_payload(teams_payload)
    machine_names = {
        team_id: {
            n
            for n in (
                directory.display_names.get(team_id, ""),
                config.managers.get(team_id, ""),
            )
            if n
        }
        for team_id in config.effective_team_ids
    }

    # Carry forward the settings ESPN knows nothing about. Regenerating is the
    # documented fix for a lost config, so it must not silently undo local
    # choices — wiping [reference].path in particular would drop the draft back
    # onto invented sample values with no symptom until the numbers looked odd.
    if args.path.is_file():
        from dataclasses import replace as _replace

        try:
            previous = load_config(args.path)
        except ConfigError:
            previous = None
        if previous is not None:
            # Nicknames are the one thing in this file that cannot be
            # regenerated from ESPN — ESPN's answer is the username you ran this
            # command to stop typing.
            # `machine_names` is what stops a stale *account handle* being
            # mistaken for a hand-set nickname. Without it, a config written
            # before real names were read would pin `macurl1392` forever:
            # `looks_generated` only catches the `espn06814226` shape, so there
            # would be no way out short of editing the file by hand.
            merged, notes = carry_forward(
                previous_managers=dict(previous.managers),
                previous_owners=dict(previous.owners),
                fresh_managers=dict(config.managers),
                fresh_owners=dict(config.owners),
                team_ids=config.effective_team_ids,
                machine_names=machine_names,
            )
            # The field list lives in `ffa.config.carry` beside a test that
            # walks every field of `LeagueConfig`. It used to be six names
            # inlined here, which meant every new setting was silently wiped by
            # the documented recovery command until somebody noticed.
            keep = carried(previous)
            keep["managers"] = merged
            keep["reference_path"] = previous.reference_path or config.reference_path
            config = _replace(config, **keep)
            kept = sum(
                1
                for tid, name in merged.items()
                if previous.managers.get(tid) == name and not looks_generated(name)
            )
            if kept:
                print(f"  kept {kept} hand-set nickname(s)")
            for note in notes:
                print(f"  {note}")
            if previous.reference_path:
                print(f"  kept [reference].path = {previous.reference_path}")

    write_config(config, args.path)

    print(f"wrote {args.path}")
    print(f"  league       {config.name or '(unnamed)'} #{config.league_id} ({config.year})")
    print(f"  draft        {config.draft_type}, ${config.budget}/team")
    print(f"  teams        {config.team_count}  ids={list(config.effective_team_ids)}")
    print(f"  you          team {config.my_team_id or 'UNSET'}"
          f"{' (' + config.managers[config.my_team_id] + ')' if config.my_team_id in config.managers else ''}")
    print(f"  roster       {config.draftable_slots} draftable slots, "
          f"{config.starting_slots} starters")
    print(f"  managers     {_manager_summary(config)}")
    print(f"  nominations  {_nomination_summary(config)}")
    _print_roster(config)

    for warning in warnings:
        print(f"! {warning}", file=sys.stderr)

    generated = sorted(t for t, n in config.managers.items() if looks_generated(n))
    if generated:
        print(
            "! teams "
            + ", ".join(str(t) for t in generated)
            + " still carry ESPN's generated usernames. Correcting a pick means "
            "typing one of those exactly. Fix it once with:  ffa config nicknames",
            file=sys.stderr,
        )

    # Not fatal here — `config check` and `draft` both enforce it — but it is
    # the one field nothing can work around, so say so plainly.
    if not config.my_team_id:
        print(
            "! set [teams].my_team_id before drafting: every projection keys "
            "off it, and advice about the wrong team is worse than none.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_config_nicknames(args: argparse.Namespace) -> int:
    """Give every manager a name you can actually type mid-draft.

    ESPN calls people `espn06814226`. That is what `[managers]` is seeded with,
    and it is unusable under a clock: correcting a misattributed pick becomes
    `sold barkley 62 espn78078705`. One pass, once, and the rest of the draft is
    `sold barkley 62 dave`.
    """
    from dataclasses import replace as _replace

    from ffa.config.loader import write_config
    from ffa.config.nicknames import check_unique, looks_generated, parse_assignment

    config = load_config(args.path)
    managers = dict(config.managers)

    if args.set:
        for assignment in args.set:
            try:
                team_id, nickname = parse_assignment(assignment)
            except ValueError as exc:
                raise ConfigError(f"--set {assignment!r}: {exc}") from None
            if team_id not in config.effective_team_ids:
                raise ConfigError(
                    f"--set {assignment!r}: team {team_id} is not in this league. "
                    "Team ids: "
                    + ", ".join(str(i) for i in config.effective_team_ids)
                )
            managers[team_id] = nickname
    elif not args.list:
        managers = _prompt_for_nicknames(config, managers)

    try:
        check_unique(managers)
    except ValueError as exc:
        raise ConfigError(f"nicknames must be unique: {exc}") from None

    changed = managers != dict(config.managers)
    if changed:
        write_config(_replace(config, managers=managers), args.path)
        print(f"wrote {args.path}")

    print(_nickname_table(config, managers))
    remaining = [t for t, n in sorted(managers.items()) if looks_generated(n)]
    missing = [t for t in config.effective_team_ids if t not in managers]
    if remaining or missing:
        print(
            "! still untypeable: "
            + ", ".join(f"team {t}" for t in sorted(set(remaining) | set(missing)))
            + "  (addressable as t<id> either way)",
            file=sys.stderr,
        )
    elif not args.list:
        print("every team has a typeable nickname.")
    return 0


def _prompt_for_nicknames(
    config: LeagueConfig, managers: dict[int, str]
) -> dict[int, str]:
    """Walk the roster of managers once, interactively.

    Blank keeps what is there, which makes a re-run cheap: the fix for one bad
    name is to run this again and press Enter eleven times.
    """
    from ffa.config.nicknames import check_nickname, looks_generated

    if not sys.stdin.isatty():
        raise ConfigError(
            "nothing to do: no --set given and stdin is not a terminal.\n"
            "Either run this in a terminal, or pass --set 3=dave --set 5=mike."
        )

    print("One word each, no spaces. Enter keeps the current value; `-` clears it.")
    print(_nickname_table(config, managers))
    print("")

    for team_id in config.effective_team_ids:
        current = managers.get(team_id, "")
        name = config.team_names.get(team_id, "")
        hint = f" [{current}]" if current and not looks_generated(current) else ""
        label = f"team {team_id}" + (f" ({name})" if name else "")
        try:
            typed = input(f"{label}{hint}: ").strip()
        except EOFError:
            print("")
            break
        if not typed:
            continue
        if typed == "-":
            managers.pop(team_id, None)
            continue
        try:
            managers[team_id] = check_nickname(typed)
        except ValueError as exc:
            print(f"  ! {exc}", file=sys.stderr)
    return managers


def _nickname_table(config: LeagueConfig, managers: dict[int, str]) -> str:
    from ffa.config.nicknames import looks_generated

    lines = []
    for team_id in config.effective_team_ids:
        nickname = managers.get(team_id, "")
        mark = "  <- ESPN username" if looks_generated(nickname) else ""
        if not nickname:
            mark = "  <- unset"
        mine = " (you)" if team_id == config.my_team_id else ""
        team_name = config.team_names.get(team_id, "")
        lines.append(
            f"  {team_id:>3}  {nickname or '-':<16}{mine:<6} {team_name}{mark}"
        )
    return "\n".join(lines)


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

    # Attach to the browser *before* touching the run directory. Doing it after
    # leaves an orphan run behind on failure — journal written, lockfile held —
    # and a later `--resume` would find that empty draft instead of the real
    # one. A failed attach should cost nothing.
    resuming = bool(args.resume or args.resume_id)
    source = _build_source(args, config, resuming=resuming)

    if resuming:
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

    # Loaded once, here, and never touched again. The nomination path is
    # uncached and has to stay instant; a disk read inside it would add latency
    # and a failure mode while a clock runs.
    precedent = _load_precedent(config, args.precedent)
    seats_map = seats(config)

    try:
        return run_repl(
            store, stdin=sys.stdin, stdout=sys.stdout,
            book=load_book(config), source=source,
            precedent=precedent, seats=seats_map,
            # Read from config, not the journal: a plan is an intention, and
            # revising it mid-draft is a legitimate thing to do.
            strategy=config.strategy,
        )
    finally:
        store.close()
        if source is not None:
            source.stop()


def _build_source(
    args: argparse.Namespace, config: LeagueConfig, *, resuming: bool = False
):
    """Resolve --source into an EventSource, or None for manual entry.

    Manual stays the default on purpose. It is the fallback that always works,
    and draft day is the wrong time to discover that a browser attach failed.
    """
    if getattr(args, "source", "manual") != "espn":
        return None

    from ffa.ingest.espn.draftroom import DraftRoomError, DraftRoomReader
    from ffa.ingest.espn.source import DraftRoomSource

    from ffa.ingest.espn.source import TeamResolver

    # Attach once here purely to fail fast — a bad port or a missing draft tab
    # should be a clear message now, not ten silent poll failures later. The
    # same snapshot pays for the team-name pre-flight below.
    reader = DraftRoomReader(port=args.cdp_port, tab_match=args.tab or "")
    try:
        reader.connect()
        preflight = reader.snapshot()
    except DraftRoomError as exc:
        raise ConfigError(
            f"{exc}\n\nManual entry still works: re-run without --source espn."
        ) from None
    finally:
        reader.close()

    print(f"attached to the draft room on port {args.cdp_port}")
    _check_board_is_live(preflight, resuming=resuming, args=args)

    resolver = TeamResolver(config.team_names, config.effective_team_ids)
    matched, unmatched = resolver.audit(preflight)
    print(f"matched {len(matched)}/{len(preflight.teams)} draft-room teams to league ids")

    # Up front, while it is still one command to fix. Finding this at pick 40
    # means forty picks credited to whoever sat in that column.
    if unmatched:
        print("", file=sys.stderr)
        print("! these draft-room teams do not match any team in your config:",
              file=sys.stderr)
        for name in unmatched:
            print(f"!   {name}", file=sys.stderr)
        print("! Picks for them would be attributed by board position, which is "
              "NOT team-id order and will be wrong.", file=sys.stderr)
        print("! Fix: quit, run `ffa config init --force`, and start again.",
              file=sys.stderr)
        if not args.ignore_unmatched_teams:
            raise ConfigError(
                "refusing to start with unmatched teams. Re-run "
                "`ffa config init --force` to pick up renames, or pass "
                "--ignore-unmatched-teams to draft anyway."
            )

    # Hand over an *unconnected* reader. Playwright's sync API is
    # greenlet-based and not thread-safe, so the connection must be made on the
    # thread that polls it; the source connects lazily on its first snapshot.
    return DraftRoomSource(
        DraftRoomReader(port=args.cdp_port, tab_match=args.tab or ""),
        interval=config.poll_interval_seconds,
        resolver=resolver,
    )


def _check_board_is_live(preflight, *, resuming: bool, args: argparse.Namespace) -> None:
    """Refuse to start a *new* draft against a board that already finished.

    A full board is not an edge case to reason about — it is a fact: this room
    is a completed draft, so it is last season's, or a practice room from
    yesterday, and it is not the one about to be run. Left alone the mistake is
    slow and expensive. The attach succeeds, the pre-flight matches every team,
    and then every one of those completed picks pours into a brand-new journal
    as if it were happening now.

    A *partly* filled board is the opposite: that is attaching mid-draft, which
    works and is worth doing. It just gets said out loud, because those picks
    are about to be recorded.
    """
    total = len(preflight.teams) * preflight.slots_per_team
    filled = preflight.filled
    if not filled:
        return

    if total and filled >= total and not resuming:
        if not getattr(args, "allow_finished_board", False):
            raise ConfigError(
                f"that draft room is already finished — {filled}/{total} picks are "
                "on the board.\n"
                "A completed board means the wrong tab: last season's draft, or a "
                "practice room. Open the room you are about to draft in.\n"
                "To record a finished board anyway, pass --allow-finished-board; "
                "to add to an existing run, use --resume."
            )
        print(
            f"! recording a finished board: {filled}/{total} picks",
            file=sys.stderr,
        )
        return

    if not resuming:
        print(
            f"! the board already shows {filled} pick(s)"
            + (f" of {total}" if total else "")
            + " — they will be recorded into this new draft as it starts.",
            file=sys.stderr,
        )


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


def cmd_data_fetch(args: argparse.Namespace) -> int:
    """Pull auction values from ESPN into a reference file.

    Replaces the manual FantasyPros export. ESPN's own consensus is arguably
    the better baseline for this league anyway: the league drafts on ESPN, so
    ESPN's number reflects what this population actually pays.
    """
    from ffa.config.loader import load_dotenv
    from ffa.ingest.espn.players import fetch_players, rows_from_payload, write_rows

    env = load_dotenv()
    league_id = args.league_id or int(env.get("ESPN_LEAGUE_ID") or 0)
    year = args.year or int(env.get("ESPN_SEASON_YEAR") or 0)
    if not league_id or not year:
        raise ConfigError(
            "need --league-id and --year (or ESPN_LEAGUE_ID / ESPN_SEASON_YEAR "
            "in .env)"
        )

    creds = None if args.public else load_credentials()
    print(f"fetching players for league {league_id} ({year})")
    players = fetch_players(
        league_id, year,
        cookies=creds.as_cookies() if creds else None,
        limit=args.limit,
    )
    rows, warnings = rows_from_payload(players, year=year)
    if not rows:
        raise ConfigError(
            f"ESPN returned {len(players)} player(s) but none carried an "
            "auction value. Nothing written."
        )

    written = write_rows(rows, args.out)
    total = sum(r["auction_value"] for r in rows)
    print(f"wrote {written} players to {args.out}")
    print(f"  values ${min(r['auction_value'] for r in rows):.2f}"
          f" - ${max(r['auction_value'] for r in rows):.2f}, ${total:,.0f} total")
    by_pos: dict[str, int] = {}
    for row in rows:
        by_pos[row["position"]] = by_pos.get(row["position"], 0) + 1
    print(f"  {', '.join(f'{p}:{n}' for p, n in sorted(by_pos.items()))}")

    for warning in warnings:
        print(f"! {warning}", file=sys.stderr)

    # Point the config at what we just wrote. Skipping this is the one mistake
    # with no visible symptom at fetch time: `load_book` falls back to the
    # sample fixture, and the draft runs on invented numbers. The REPL does
    # warn, but a banner you have to notice is a weaker guarantee than a config
    # that is simply correct.
    if args.set_path and args.config.is_file():
        from ffa.config.loader import write_config
        from dataclasses import replace as _replace

        current = load_config(args.config)
        if current.reference_path != str(args.out):
            write_config(_replace(current, reference_path=str(args.out)), args.config)
            print(f"  set [reference].path in {args.config}")

    print(f"\nvalidate it with:  ffa data validate {args.out}")
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

    config = load_config(args.config) if args.config.is_file() else None
    book = load_book(config) if config is not None else None
    dossiers = _load_dossiers(config, args.dossiers)
    precedent = _load_precedent(config, args.precedent) if config is not None else None
    view = build_view(
        replay(events, directory.name), book, dossiers=dossiers,
        precedent=precedent, seats=seats(config) if config is not None else None,
        strategy=config.strategy if config is not None else None,
    )
    text = json.dumps(view, indent=2)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


# --- helpers ------------------------------------------------------------------


def _load_precedent(config: LeagueConfig, path: Path):
    """Each manager's own spending script, or nothing at all.

    Never fatal. Everything else works without it — this only adds a line to the
    bid readout — and refusing to start a draft over a derived file would be
    absurd.
    """
    from ffa.history.precedent import load_precedent

    book = load_precedent(path, league_id=config.league_id)
    for warning in book.warnings:
        print(f"! {warning}", file=sys.stderr)
    if not book:
        print(
            "no draft history loaded — bid guidance will not compare rivals to "
            "their own past drafts. Build it with:  ffa history report",
            file=sys.stderr,
        )
    else:
        print(
            f"history: {len(book)} manager(s) from "
            f"{', '.join(str(y) for y in book.seasons)}"
        )
    return book


def _load_dossiers(config: LeagueConfig | None, path: Path):
    """What we know about the managers, or nothing at all.

    Never fatal, and deliberately quiet when the file is simply absent: the
    whole deterministic layer works without a single dossier, and refusing to
    export a payload over a missing notes file would be absurd.
    """
    if config is None:
        return None
    from ffa.dossier.store import load_dossiers

    book = load_dossiers(path, owners=seats(config))
    for warning in book.warnings:
        print(f"! {warning}", file=sys.stderr)
    return book if len(book) else None


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
        nomination_order=tuple(config.nomination_order),
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


def _print_roster(config: LeagueConfig) -> None:
    """Who is in which seat, under all three names.

    None of the three is sufficient alone, which is the lesson of the bug this
    was written for. The nickname is what you type, the real name is who they
    are, the team name is what you see on the draft board — and for a long time
    only ESPN's account handle reached any surface, so the roster read
    `macurl1392` while "Michael Curley" sat unused in the same payload.
    """
    if not (config.real_names or config.team_names):
        return
    print("  who is who")
    for team_id in config.effective_team_ids:
        nickname = config.managers.get(team_id, "-")
        real = config.real_names.get(team_id, "") or "(no name on ESPN)"
        team_name = config.team_names.get(team_id, "")
        mine = "*" if team_id == config.my_team_id else " "
        print(f"   {mine}{team_id:>3}  {nickname:<14} {real:<24} {team_name}")


def _nomination_summary(config) -> str:
    """Who nominates in what order, in the names you actually type."""
    if not config.nomination_order:
        return "unknown (ESPN published no pickOrder)"
    names = [config.managers.get(t, f"t{t}") for t in config.nomination_order]
    mine = config.nomination_order.index(config.my_team_id) + 1 if (
        config.my_team_id in config.nomination_order
    ) else 0
    seat = f"  (you nominate {mine} of {len(names)})" if mine else ""
    return " -> ".join(names) + seat


def _strategy_summary(config: LeagueConfig) -> str:
    """C2 in one line. Says nothing rather than implying a default plan."""
    preset = config.strategy
    if not preset.is_set:
        return "none declared (`ffa strategy init` builds one)"
    split = ", ".join(
        f"{position.value} ${dollars}"
        for position, dollars in sorted(
            preset.budget_by_position.items(), key=lambda kv: -kv[1]
        )
    )
    cap = f"max ${preset.max_on_one_player} on one" if preset.max_on_one_player else ""
    parts = [p for p in (preset.archetype or "custom", cap, split) if p]
    return "  ".join(parts)


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

    init = config_sub.add_parser("init", help="generate config/league.toml from ESPN")
    init.add_argument("--league-id", type=int, default=None, help="(or set ESPN_LEAGUE_ID)")
    init.add_argument("--year", type=int, default=None, help="(or set ESPN_SEASON_YEAR)")
    init.add_argument("--path", type=Path, default=DEFAULT_CONFIG_PATH)
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    init.add_argument("--public", action="store_true", help="skip cookie auth (public leagues)")
    init.set_defaults(func=cmd_config_init)

    from ffa.cli.alias_cmd import add_parser as add_alias_parser

    add_alias_parser(config_sub)

    nick = config_sub.add_parser(
        "nicknames",
        help="name each manager something you can type mid-draft",
    )
    nick.add_argument("--path", type=Path, default=DEFAULT_CONFIG_PATH)
    nick.add_argument("--set", action="append", metavar="ID=NAME", default=[],
                      help="set one non-interactively; repeatable")
    nick.add_argument("--list", action="store_true", help="show them and change nothing")
    nick.set_defaults(func=cmd_config_nicknames)

    draft = sub.add_parser("draft", help="run a live draft session")
    draft.add_argument("--new", action="store_true", help="start a new draft")
    draft.add_argument("--resume", action="store_true", help="resume the most recent draft")
    draft.add_argument("--resume-id", metavar="DRAFT_ID", help="resume a specific draft")
    draft.add_argument("--source", choices=["manual", "espn"], default="manual",
                       help="where picks come from. espn reads the draft room you "
                            "have open in the Chrome started by "
                            "tools/draft_room_probe.py --launch")
    draft.add_argument("--cdp-port", type=int, default=9222,
                       help="Chrome remote-debugging port for --source espn")
    draft.add_argument("--tab", default="",
                       help="substring of the draft-room tab's URL, when more "
                            "than one draft room is open")
    draft.add_argument("--allow-finished-board", action="store_true",
                       help="start a new draft against a board that is already "
                            "complete (normally refused: it means the wrong tab)")
    draft.add_argument("--ignore-unmatched-teams", action="store_true",
                       help="start even if draft-room team names do not match "
                            "the config (picks for them may be misattributed)")
    draft.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    draft.add_argument("--runs", type=Path, default=RUNS_DIR)
    draft.add_argument("--precedent", type=Path,
                       default=Path("data/manager_precedent.json"),
                       help="spending scripts from prior drafts; absent is fine")
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

    fetch = data_sub.add_parser("fetch", help="pull auction values from ESPN")
    fetch.add_argument("--out", type=Path, default=Path("data/reference/espn_values.csv"))
    fetch.add_argument("--league-id", type=int, default=None)
    fetch.add_argument("--year", type=int, default=None)
    fetch.add_argument("--limit", type=int, default=900, help="player universe size to request")
    fetch.add_argument("--public", action="store_true", help="skip cookie auth")
    fetch.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    fetch.add_argument("--no-set-path", dest="set_path", action="store_false",
                       help="don't point [reference].path at the file just written")
    fetch.set_defaults(func=cmd_data_fetch)

    from ffa.cli.dashboard_cmd import add_parser as add_dashboard_parser
    from ffa.cli.dossier_cmd import add_parser as add_dossier_parser
    from ffa.cli.history_cmd import add_parser as add_history_parser
    from ffa.cli.strategy_cmd import add_parser as add_strategy_parser

    add_dossier_parser(sub)
    add_history_parser(sub)
    add_strategy_parser(sub)
    add_dashboard_parser(sub)

    export = sub.add_parser("export-state", help="write the JSON view model for a run")
    export.add_argument("--run-dir", type=Path, default=None)
    export.add_argument("--runs", type=Path, default=RUNS_DIR)
    export.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    export.add_argument("--dossiers", type=Path, default=Path("data/dossiers.json"))
    export.add_argument("--precedent", type=Path,
                        default=Path("data/manager_precedent.json"))
    export.add_argument("--out", type=Path, default=None)
    export.set_defaults(func=cmd_export_state)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, CommandError, DossierError, JournalError, LockError,
            ReferenceError) as exc:
        # All user-fixable; a traceback would only bury the message.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # The live loop catches its own Ctrl-C, but only while it is parked on
        # the queue. One pressed during a redraw, inside the source thread's
        # shutdown, or a second one landing during the first one's cleanup, all
        # arrive here instead. A rehearsal ended on a KeyboardInterrupt
        # traceback, which reads like lost work and is not: every event is
        # fsynced before the next prompt appears, so there is nothing in flight
        # to lose. Say that, rather than printing a stack.
        print("\nstopped. everything is saved.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
