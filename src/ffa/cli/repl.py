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

from ffa.cli import render
from ffa.domain.models import DraftState
from ffa.ingest.manual.errors import CommandError
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
) -> int:
    """Drive a draft until quit, EOF, or Ctrl-C. Returns an exit code."""
    emit = _writer(stdout)

    if store.load_warnings:
        emit(render.render_warnings(store.load_warnings))
    emit(_banner(store.state))

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
                line, ParseContext(state=store.state, now=now_fn(), events=store.events)
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
            emit(_view(store, command))
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


def _emit_new_warnings(emit, state: DraftState, seen: int) -> int:
    if len(state.warnings) > seen:
        emit(render.render_warnings(state.warnings[seen:]))
    return len(state.warnings)


def _view(store: DraftStore, command: ViewCommand) -> str:
    state = store.state
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


def _writer(stdout: TextIO):
    def emit(text: str) -> None:
        if text:
            stdout.write(text + "\n")
            stdout.flush()

    return emit
