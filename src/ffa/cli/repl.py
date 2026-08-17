"""The draft loop.

Reads from any `TextIO`, not just stdin. That is deliberate: it means an
annotated session transcript can be piped in, which is how the integration
tests drive a complete draft with no pty tricks — and it's also how you'd
replay a session to reproduce a bug.

Nothing is buffered. Every event is fsynced before the next prompt appears, so
Ctrl-C, a closed laptop, or a kernel panic all lose exactly nothing.
"""

from __future__ import annotations

from typing import Callable, TextIO

from ffa.advice.engine import advise
from ffa.cli import render
from ffa.domain.events import PlayerNominated
from ffa.domain.models import DraftState
from ffa.ingest.manual.errors import CommandError
from ffa.ingest.manual.resolve import resolve_player
from ffa.ingest.manual.grammar import (
    EmitCommand,
    NoopCommand,
    ParseContext,
    QuitCommand,
    ViewCommand,
    parse_command,
)
from ffa.state.store import DraftStore
from ffa.util.clock import now_utc

PROMPT = "> "


def run_repl(
    store: DraftStore,
    *,
    stdin: TextIO,
    stdout: TextIO,
    now_fn: Callable[[], object] = now_utc,
    prompt: str = PROMPT,
    book=None,
) -> int:
    """Drive a draft until quit, EOF, or Ctrl-C. Returns an exit code."""
    emit = _writer(stdout)

    if store.load_warnings:
        emit(render.render_warnings(store.load_warnings))
    emit(_banner(store.state))
    emit(_reference_banner(book))

    seen_warnings = len(store.state.warnings)

    while True:
        stdout.write(prompt)
        stdout.flush()
        try:
            line = stdin.readline()
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            emit("\nstopped. everything is saved.")
            return 0

        if line == "":  # EOF
            emit("")
            return 0

        try:
            command = parse_command(
                line,
                ParseContext(
                    state=store.state, now=now_fn(), events=store.events, book=book
                ),
            )
        except CommandError as exc:
            emit(f"error: {exc}")
            continue

        if isinstance(command, NoopCommand):
            continue
        if isinstance(command, QuitCommand):
            emit("saved.")
            return 0
        if isinstance(command, ViewCommand):
            emit(_view(store, command, book))
            continue

        if isinstance(command, EmitCommand):
            try:
                for event in command.events:
                    store.dispatch(event)
            except Exception as exc:  # pragma: no cover - journal failures only
                emit(f"error: could not record that: {exc}")
                continue
            # Echo what was written, with the id that `undo #id` takes.
            emit(f"#{store.events[-1].id} {command.echo}")
            seen_warnings = _emit_new_warnings(emit, store.state, seen_warnings)

            # Capability 2: the bid readout auto-fires on nomination. That is
            # the moment it's needed, and asking for it costs seconds you
            # don't have while the auctioneer is counting.
            if book is not None and store.state.current_nomination is not None:
                if any(isinstance(e, PlayerNominated) for e in command.events):
                    emit(render.render_guidance(advise(store.state, book).guidance))


def _emit_new_warnings(emit, state: DraftState, seen: int) -> int:
    if len(state.warnings) > seen:
        emit(render.render_warnings(state.warnings[seen:]))
    return len(state.warnings)


def _view(store: DraftStore, command: ViewCommand, book=None) -> str:
    state = store.state
    if command.kind in ("advice", "scarcity", "market"):
        if book is None or not len(book):
            return (
                "no reference data loaded.\n"
                "Set [reference].path in your config to your exported auction "
                "values, then restart."
            )
        if command.kind == "scarcity":
            return render.render_scarcity(advise(state, book).scarcity)
        if command.kind == "market":
            return render.render_market(advise(state, book).market)

        key = None
        if command.arg:
            try:
                key = resolve_player(command.arg, book).key
            except CommandError as exc:
                return f"error: {exc}"
        elif state.current_nomination is None:
            return "nothing nominated. Try: advice <player>"
        return render.render_guidance(advise(state, book, key=key).guidance)

    if command.kind == "budgets":
        team_id = _team_arg(state, command.arg)
        return render.render_budgets(state, team_id)
    if command.kind == "state":
        return render.render_state(state, command.arg)
    if command.kind == "log":
        limit = int(command.arg) if command.arg and command.arg.isdigit() else 12
        return render.render_log(store.events, limit)
    return render.render_help(command.arg)


def _team_arg(state: DraftState, arg: str | None) -> int | None:
    if not arg:
        return None
    from ffa.ingest.manual.resolve import resolve_team

    try:
        return resolve_team(state, arg)
    except CommandError:
        return None


def _banner(state: DraftState) -> str:
    if not state.is_initialized:
        return "draft not initialized"
    league = state.league
    sold = len(state.sold_players())
    return (
        f"{league.name or 'draft'} — {league.team_count} teams, ${league.budget} each, "
        f"{league.draftable_slots} roster spots\n"
        f"{sold} pick(s) recorded. Type 'help' for commands, 'quit' to stop."
    )


def _reference_banner(book) -> str:
    if book is None or not len(book):
        return "no reference data — bid advice is unavailable (set [reference].path)"
    if book.is_sample:
        # Loud on purpose. Sample values are invented; acting on them would be
        # worse than having no advice at all.
        return (
            f"USING SAMPLE DATA ({len(book)} players) — these values are INVENTED. "
            "Set [reference].path to your real export before draft day."
        )
    return f"reference: {len(book)} players from {book.source}"


def _writer(stdout: TextIO):
    def emit(text: str) -> None:
        if text:
            stdout.write(text + "\n")
            stdout.flush()

    return emit
