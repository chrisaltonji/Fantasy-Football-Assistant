"""`ffa dossier` — recording what you know about the twelve people in the room.

Kept out of `app.py` on purpose: that file is meant to be a thin dispatcher, and
an interview loop is neither thin nor about argument parsing.

The commands, in the order you use them:

    ffa dossier init                  seed one entry per seat from your config
    ffa dossier brief                 a prompt you paste into a chat, to talk it through
    ffa dossier form                  a fill-in questionnaire for the couch
    ffa dossier interview --team 3    type the answers in
    ffa dossier import answers.json   take back what the chat produced
    ffa dossier show --team 3         read one back
    ffa dossier status                who is covered, who is not

`brief` + `import` exist because the interview is *recall*, and recall goes
better spoken than typed. Whatever the front end — a chat, voice, a note on a
phone — the answers come back as JSON and go through exactly the same validation
the terminal interview uses.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from ffa.config.loader import load_config
from ffa.config.schema import ConfigError, LeagueConfig
from ffa.dossier.brief import render_brief
from ffa.dossier.form import render_form
from ffa.dossier.ingest import apply_payload, extract_payload
from ffa.dossier.schema import (
    QUESTIONS,
    DossierError,
    OwnerDossier,
    parse_answer,
    render_answer,
    seed,
)
from ffa.dossier.store import (
    DEFAULT_DOSSIER_PATH,
    DossierBook,
    load_dossiers,
    save_dossiers,
)
from ffa.util.clock import now_utc


def _seats(config: LeagueConfig) -> dict[int, str]:
    """`team_id -> SWID`, for every seat we can identify.

    A team with no recorded owner cannot hold a dossier, because there is
    nothing stable to attach it to. Saying so beats inventing a key that breaks
    the moment somebody leaves the league.
    """
    return {
        team_id: config.owners[team_id]
        for team_id in config.effective_team_ids
        if config.owners.get(team_id)
    }


def _book(config: LeagueConfig, path: Path) -> DossierBook:
    book = load_dossiers(path, owners=_seats(config))
    for warning in book.warnings:
        print(f"! {warning}", file=sys.stderr)
    return book


def _require_seats(config: LeagueConfig) -> dict[int, str]:
    seats = _seats(config)
    if not seats:
        raise ConfigError(
            "your config records no owner SWIDs, so there is nothing stable to "
            "attach a dossier to.\nRun `ffa config init` to pull them from ESPN."
        )
    return seats


def _resolve_team(config: LeagueConfig, arg: str | None) -> int | None:
    """`3`, `t3`, or a manager nickname."""
    if not arg:
        return None
    text = str(arg).strip().lower().lstrip("t")
    if text.isdigit() and int(text) in config.effective_team_ids:
        return int(text)

    matches = [
        team_id
        for team_id, nickname in config.managers.items()
        if str(nickname).lower().startswith(str(arg).strip().lower())
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigError(
            f"{arg!r} matches more than one manager: "
            + ", ".join(config.managers[t] for t in sorted(matches))
        )
    raise ConfigError(
        f"no team matches {arg!r}. Team ids: "
        + ", ".join(str(i) for i in config.effective_team_ids)
    )


# --- commands --------------------------------------------------------------------


def cmd_dossier_init(args: argparse.Namespace) -> int:
    """Seed an empty dossier per seat, so the file mirrors the league."""
    config = load_config(args.config)
    seats = _require_seats(config)

    if args.path.is_file() and not args.force:
        raise ConfigError(
            f"{args.path} already exists. `ffa dossier interview` adds to it; "
            "use --force only if you mean to start over."
        )

    book = DossierBook({}, owners=seats)
    for team_id, owner_id in sorted(seats.items()):
        book = book.put(
            seed(owner_id, team_id=team_id, label=config.managers.get(team_id, ""))
        )
    save_dossiers(book, args.path, league_id=config.league_id)

    print(f"wrote {args.path} — {len(seats)} empty dossier(s)")
    missing = [t for t in config.effective_team_ids if t not in seats]
    if missing:
        print(
            "! no owner SWID for team(s) "
            + ", ".join(str(t) for t in missing)
            + " — they cannot hold a dossier. Re-run `ffa config init`.",
            file=sys.stderr,
        )
    print("\nNext:  ffa dossier form      (fill it in away from the keyboard)")
    print("  or:  ffa dossier interview  (answer at the prompt)")
    return 0


def cmd_dossier_form(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    seats = _require_seats(config)
    book = _book(config, args.path)

    text = render_form(book, seats=seats, labels=dict(config.managers))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")

    print(f"wrote {args.out} — {len(seats)} manager(s), {len(QUESTIONS)} questions each")
    print("Fill it in, then type it back with:  ffa dossier interview")
    return 0


def cmd_dossier_brief(args: argparse.Namespace) -> int:
    """Write a prompt that turns the interview into a conversation.

    Thirteen questions across twelve managers is 156 prompts, and a terminal is
    the wrong shape for that. Paste this into a chat, talk it through, and bring
    the answers back with `ffa dossier import`.
    """
    config = load_config(args.config)
    seats = _require_seats(config)
    book = _book(config, args.path)

    text = render_brief(
        book, seats=seats, labels=dict(config.managers), league_name=config.name
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")

    print(f"wrote {args.out}")
    print(f"  {len(seats)} manager(s), {len(QUESTIONS)} questions each")
    print("\nPaste the whole file into a chat and talk it through. When it hands "
          "you a\n```json block, save it and run:  ffa dossier import <file>")
    return 0


def cmd_dossier_import(args: argparse.Namespace) -> int:
    """Merge answers from a conversation back into the record.

    Everything goes through the same `parse_answer` the terminal interview uses.
    An import path with its own looser validation would be a second way to get a
    bad value into the one file here that cannot be regenerated.
    """
    config = load_config(args.config)
    seats = _require_seats(config)
    book = _book(config, args.path)

    raw = sys.stdin.read() if str(args.source) == "-" else _read(args.source)
    payload = extract_payload(raw)

    updated, report = apply_payload(
        book,
        payload,
        seats=seats,
        labels=dict(config.managers),
        today=now_utc().date().isoformat(),
    )

    for problem in report.rejected:
        print(f"! {problem}", file=sys.stderr)

    if not report:
        print("nothing to apply — no valid answers found.")
        return 1

    for team_id in report.changed_teams:
        label = config.managers.get(team_id) or f"team{team_id}"
        fields = ", ".join(report.applied[team_id])
        print(f"  team {team_id:<3} {label:<16} {fields}")

    if args.dry_run:
        print(f"\n--dry-run: {report.field_count} answer(s) across "
              f"{len(report.changed_teams)} manager(s) would be written. "
              f"Nothing changed.")
        return 0

    save_dossiers(updated, args.path, league_id=config.league_id)
    print(f"\nwrote {args.path} — {report.field_count} answer(s) across "
          f"{len(report.changed_teams)} manager(s)")
    print(f"{updated.coverage(seats):.0%} of the league now has some read on them.")
    return 0


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from None


def cmd_dossier_show(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    book = _book(config, args.path)
    seats = _seats(config)

    wanted = _resolve_team(config, args.team)
    team_ids = [wanted] if wanted else sorted(seats)
    if not team_ids:
        print("no dossiers yet. Start with:  ffa dossier init")
        return 1

    for team_id in team_ids:
        dossier = book.for_team(team_id) or OwnerDossier(owner_id=seats.get(team_id, ""))
        label = config.managers.get(team_id) or dossier.label or f"team{team_id}"
        print(f"\nteam {team_id} — {label}"
              f"   ({len(dossier.answered)}/{len(QUESTIONS)} answered)")
        for question in QUESTIONS:
            value = render_answer(question, getattr(dossier, question.field, None))
            print(f"  {question.field:<18} {value}")
        if dossier.updated_at:
            print(f"  {'updated':<18} {dossier.updated_at}")
    return 0


def cmd_dossier_status(args: argparse.Namespace) -> int:
    """Who is covered, and what the gaps are.

    Coverage is worth showing plainly: a bid forecast built on four dossiers out
    of twelve is a different thing from one built on all twelve, and only this
    command makes that visible before draft day rather than during it.
    """
    config = load_config(args.config)
    seats = _seats(config)
    book = _book(config, args.path)

    if not seats:
        print("no owner SWIDs in your config — run `ffa config init`.")
        return 1

    print(f"{args.path}   {len(book)} entr(ies)")
    print(f"  {'TEAM':<6} {'MANAGER':<14} {'ANSWERED':<10} MISSING")
    for team_id in sorted(seats):
        dossier = book.for_team(team_id)
        label = config.managers.get(team_id) or (dossier.label if dossier else "") or "-"
        answered = len(dossier.answered) if dossier else 0
        missing = ", ".join(dossier.missing) if dossier else "everything"
        mark = "*" if team_id == config.my_team_id else " "
        print(f" {mark}{team_id:<5} {label:<14} {answered:>2}/{len(QUESTIONS):<7} "
              f"{missing[:60]}")

    covered = book.coverage(seats)
    print(f"\n{covered:.0%} of the league has some read on them.")
    if covered < 1.0:
        print("Bid forecasting reads these directly, so the gaps are where it "
              "will have nothing to say.")
    return 0


def cmd_dossier_interview(args: argparse.Namespace) -> int:
    """Walk the questions. Enter skips, `-` clears, `?` explains why it matters."""
    config = load_config(args.config)
    seats = _require_seats(config)
    book = _book(config, args.path)

    if not sys.stdin.isatty():
        raise ConfigError(
            "an interview needs a terminal.\nTo fill dossiers in without one, "
            "run `ffa dossier form` and edit data/dossiers.json directly."
        )

    wanted = _resolve_team(config, args.team)
    team_ids = [wanted] if wanted else sorted(seats)

    print("Enter skips a question. `-` clears an answer. `?` says why it matters.")
    print("Ctrl-C stops; everything answered so far is saved.\n")

    changed = 0
    try:
        for team_id in team_ids:
            owner_id = seats[team_id]
            current = book.for_owner(owner_id) or seed(
                owner_id, team_id=team_id, label=config.managers.get(team_id, "")
            )
            label = config.managers.get(team_id) or current.label or f"team{team_id}"
            print(f"--- team {team_id} — {label} " + "-" * 30)

            updated, touched = _ask_all(current)
            if touched:
                changed += 1
                book = book.put(
                    replace(updated, team_id=team_id,
                            label=config.managers.get(team_id, current.label),
                            updated_at=now_utc().date().isoformat())
                )
                # Saved after every manager, not at the end. A twenty-minute
                # interview should never be lost to a stray Ctrl-C.
                save_dossiers(book, args.path, league_id=config.league_id)
            print("")
    except (KeyboardInterrupt, EOFError):
        print("\nstopped. everything answered so far is saved.")

    print(f"{changed} dossier(s) updated in {args.path}")
    return 0


def _ask_all(dossier: OwnerDossier) -> tuple[OwnerDossier, bool]:
    touched = False
    for question in QUESTIONS:
        while True:
            current = render_answer(question, getattr(dossier, question.field, None))
            hint = f" [{current}]" if current != "-" else ""
            if question.choices:
                keys = "/".join(c.key for c in question.choices)
                print(f"  {question.prompt}  ({keys})")
            else:
                print(f"  {question.prompt}")

            typed = input(f"  {question.field}{hint}: ").strip()
            if not typed:
                break
            if typed == "?":
                print(f"    why: {question.why}")
                for choice in question.choices:
                    print(f"    {choice.key:<18} {choice.means}")
                continue
            if typed == "-":
                dossier = replace(dossier, **{question.field: _blank(question.kind)})
                touched = True
                break
            try:
                dossier = replace(
                    dossier, **{question.field: parse_answer(question, typed)}
                )
            except DossierError as exc:
                print(f"    ! {exc}", file=sys.stderr)
                continue
            touched = True
            break
    return dossier, touched


def _blank(kind: str):
    if kind in ("positions", "nfl_teams"):
        return ()
    if kind == "text":
        return ""
    return None


# --- parser ------------------------------------------------------------------------


def add_parser(sub) -> None:
    from ffa.config.loader import DEFAULT_CONFIG_PATH

    dossier = sub.add_parser(
        "dossier", help="what you know about the managers, for bid forecasting"
    )
    dossier_sub = dossier.add_subparsers(dest="dossier_command", required=True)

    def common(parser):
        parser.add_argument("--path", type=Path, default=DEFAULT_DOSSIER_PATH)
        parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
        return parser

    init = common(dossier_sub.add_parser("init", help="seed one empty dossier per seat"))
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(func=cmd_dossier_init)

    brief = common(dossier_sub.add_parser(
        "brief", help="write a prompt you can paste into a chat and talk through"
    ))
    brief.add_argument("--out", type=Path, default=Path("docs/dossier_brief.md"))
    brief.set_defaults(func=cmd_dossier_brief)

    form = common(dossier_sub.add_parser(
        "form", help="write a fill-in questionnaire you can do away from a terminal"
    ))
    form.add_argument("--out", type=Path, default=Path("docs/dossier_form.md"))
    form.set_defaults(func=cmd_dossier_form)

    imp = common(dossier_sub.add_parser(
        "import", help="merge answers from a chat or a JSON file into the record"
    ))
    imp.add_argument("source", type=Path,
                     help="a .json file, or a transcript with a ```json block. "
                          "`-` reads stdin.")
    imp.add_argument("--dry-run", action="store_true",
                     help="say what would change and write nothing")
    imp.set_defaults(func=cmd_dossier_import)

    interview = common(dossier_sub.add_parser(
        "interview", help="answer the questions at the prompt"
    ))
    interview.add_argument("--team", default=None,
                           help="one team id or manager nickname; default is all")
    interview.set_defaults(func=cmd_dossier_interview)

    show = common(dossier_sub.add_parser("show", help="read a dossier back"))
    show.add_argument("--team", default=None)
    show.set_defaults(func=cmd_dossier_show)

    status = common(dossier_sub.add_parser("status", help="coverage across the league"))
    status.set_defaults(func=cmd_dossier_status)
