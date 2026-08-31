"""`ffa strategy` — declare the plan, and see how it is holding up.

Two commands and no more. `init` writes a plan you can edit; `show` tells you
whether you are keeping it. The plan lives in `config/league.toml` under
`[strategy]`, which is deliberate: it is a setting you own, `config check`
validates it alongside everything else, and `config init --force` carries it
forward rather than wiping it.

The reason `init` exists at all rather than a documented blank table: nobody
hand-writes a six-position dollar split from nothing the week of a draft. It
builds one off the same reference sheet the bid advice already trusts, so what
you are doing afterwards is disagreeing with specific numbers instead of
inventing them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ffa.config.loader import DEFAULT_CONFIG_PATH, load_config, write_config
from ffa.config.schema import ConfigError
from ffa.config.strategy import (
    ARCHETYPES,
    BY_NAME,
    bench_floor,
    default_preset,
    opening_max_bid,
)


def _load_book(config):
    from ffa.cli.app import load_book

    return load_book(config)


def _print_slots(preset) -> None:
    """The plan as a lineup, in roster order, with a bar you can read at a glance.

    Roster order rather than by size: these are the ten spots you have to fill,
    and a list that reorders itself as you edit is one you have to re-find every
    time you look at it. The bar is proportional to the largest slot, so the
    *shape* of the plan - which is the only thing the archetype decides - is
    visible without doing arithmetic on the numbers beside it.
    """
    slots = getattr(preset, "budget_by_slot", None)
    if not slots:
        return
    widest = max(slots.values()) or 1
    for key, dollars in slots.items():
        bar = "#" * max(0, round(dollars / widest * 24))
        note = "   flex money, pulled from RB/WR/TE" if key == "FLEX" else ""
        print(f"      {key:<5} ${dollars:<4} {bar}{note}")


def cmd_strategy_init(args: argparse.Namespace) -> int:
    config = load_config(args.path)

    if config.strategy.is_set and not args.force:
        raise ConfigError(
            "a plan is already declared in this config. Re-run with --force to "
            "replace it, or edit [strategy] by hand — `ffa strategy show` "
            "prints what you currently have."
        )

    book = _load_book(config)
    if book is None or not len(book):
        raise ConfigError(
            "no reference data loaded, so there is no market to build a plan "
            "from. Run `ffa data fetch` first."
        )

    floor = bench_floor(config)
    if args.bench is not None and args.bench < floor:
        raise ConfigError(
            f"--bench {args.bench} is below the ${floor} floor: this league has "
            f"{floor} bench slot(s) and each costs at least $1. A plan that "
            "reserves less than that cannot be filled."
        )

    ceiling = opening_max_bid(config)
    if args.max_player is not None and args.max_player > ceiling:
        raise ConfigError(
            f"--max-player {args.max_player} is above ${ceiling}, the most anyone "
            f"can legally bid on their first player: the other "
            f"{config.draftable_slots - 1} roster slots still cost $1 each. A cap "
            "above that can never bind."
        )
    if args.max_player is not None and args.max_player < 1:
        raise ConfigError("--max-player must be at least $1")

    preset = default_preset(
        config, book, args.archetype,
        bench_reserve=args.bench, max_on_one_player=args.max_player,
    )
    from dataclasses import replace

    write_config(replace(config, strategy=preset), args.path)

    shape = BY_NAME[args.archetype]
    print(f"wrote {args.path}")
    print(f"  archetype    {preset.archetype} — {shape.note}")
    print(f"  one player   at most ${preset.max_on_one_player}"
          f"   (legal ceiling ${ceiling})")
    at_floor = " (the $1-per-slot floor)" if preset.bench_reserve == floor else ""
    print(f"  bench        ${preset.bench_reserve} held back{at_floor}")
    print(f"  starters     ${preset.planned_total} across "
          f"{len(preset.budget_by_slot)} slot(s):")
    _print_slots(preset)
    print()
    print("The split across positions comes from the market's own shape; the "
          "split *within* a position comes from the archetype's concentration, "
          "which is the only thing it has ever meant. Edit "
          "[strategy.budget_by_slot] to make them yours.")

    if book.is_sample:
        print(
            "! built from SAMPLE reference values, which are invented. Run "
            "`ffa data fetch` and rebuild the plan before draft day.",
            file=sys.stderr,
        )
    return 0


def cmd_strategy_show(args: argparse.Namespace) -> int:
    """The plan, and adherence to it if there is a draft to measure against."""
    from ffa.cli import render
    from ffa.advice.strategy import strategy_read
    from ffa.state.recovery import latest_run, load_run
    from ffa.domain.reducers import replay

    config = load_config(args.path)
    if not config.strategy.is_set:
        print(render.render_plan(None))
        return 0

    directory = args.run_dir or latest_run(args.runs)
    if directory is None:
        # No draft yet, so there is nothing to be off-plan against. Print the
        # plan itself rather than a table of zeroes pretending to be a reading.
        preset = config.strategy
        print(f"plan: {preset.archetype or 'custom'}   (no draft started yet)")
        print(f"  one player   at most ${preset.max_on_one_player}")
        print(f"  bench        ${preset.bench_reserve} held back")
        _print_slots(preset)
        if not preset.budget_by_slot:
            # A plan declared before slots existed. Positions are all it has.
            for position, dollars in sorted(
                preset.by_position.items(), key=lambda kv: -kv[1]
            ):
                print(f"  {position.value:<5} ${dollars}")
        return 0

    events, warnings = load_run(directory)
    for warning in warnings:
        print(f"! {warning}", file=sys.stderr)

    state = replay(events, directory.name)
    print(f"run: {directory.name}")
    print(render.render_plan(strategy_read(state, _load_book(config), config.strategy)))
    return 0


def add_parser(sub) -> None:
    parser = sub.add_parser("strategy", help="your draft plan, and how it is holding up")
    strategy_sub = parser.add_subparsers(dest="strategy_command", required=True)

    names = [a.name for a in ARCHETYPES]
    init = strategy_sub.add_parser(
        "init", help="build a plan from the market, for you to edit"
    )
    init.add_argument(
        "--archetype", default="balanced", choices=names,
        help="; ".join(f"{a.name}: {a.note}" for a in ARCHETYPES),
    )
    init.add_argument("--max-player", dest="max_player", type=int, default=None,
                      metavar="DOLLARS",
                      help="the most you intend to spend on any one player, "
                           "instead of the archetype's share")
    init.add_argument("--bench", type=int, default=None, metavar="DOLLARS",
                      help="hold back this much for the bench instead of the "
                           "archetype's share; the difference goes back into the "
                           "positional split at market shares")
    init.add_argument("--path", type=Path, default=DEFAULT_CONFIG_PATH)
    init.add_argument("--force", action="store_true", help="replace an existing plan")
    init.set_defaults(func=cmd_strategy_init)

    show = strategy_sub.add_parser("show", help="the plan, and adherence to it")
    show.add_argument("--path", type=Path, default=DEFAULT_CONFIG_PATH)
    show.add_argument("--run-dir", type=Path, default=None)
    show.add_argument("--runs", type=Path, default=Path("runs"))
    show.set_defaults(func=cmd_strategy_show)
