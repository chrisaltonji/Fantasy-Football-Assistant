"""The terminal line, when two things want to write to it.

This is the debt the rehearsal found at pick 14: a command typed while a pick
lands comes out garbled. The tty echoes keystrokes wherever the cursor happens
to be, so printing a sale mid-word leaves the visible command and the buffered
one disagreeing — and there is no way to tell from the screen which is which.

`RawConsole` takes the echo away from the terminal so it always knows what is on
the input line. The assertions below are about exactly that: after something
prints, is your half-typed command still there, and still on its own line.
"""

from __future__ import annotations

import io
import queue
import threading
import time

import pytest

from ffa.cli.console import (
    ConsoleUnavailable,
    PlainConsole,
    RawConsole,
    make_console,
)


class FakeKeys:
    """A scripted keyboard. `read` returns one character per call, then EOF."""

    def __init__(self, script: str = "") -> None:
        self.pending = list(script)
        self.restored = False

    def read(self) -> str:
        if not self.pending:
            return "\x04"  # Ctrl-D, which is EOF to the console
        return self.pending.pop(0)

    def restore(self) -> None:
        self.restored = True


class BlockingKeys:
    """A keyboard that waits for the next keystroke, like a real one.

    `FakeKeys` returns EOF once its script runs out, which is right for a
    one-shot read and wrong for the collision test — there the reader has to
    still be sitting on the input line when the notice arrives.
    """

    def __init__(self) -> None:
        self._pending: "queue.Queue[str]" = queue.Queue()
        self.restored = False

    def type(self, text: str) -> None:
        for ch in text:
            self._pending.put(ch)

    def read(self) -> str:
        return self._pending.get()

    def restore(self) -> None:
        self.restored = True


def _wait_for(out: io.StringIO, text: str, timeout: float = 5.0) -> None:
    """Wait for the reader thread to catch up, without a bare sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if out.getvalue().endswith(text):
            return
        time.sleep(0.005)
    raise AssertionError(f"never saw {text!r}; screen was {out.getvalue()!r}")


def raw(script: str = "", prompt: str = "> ") -> tuple[RawConsole, io.StringIO, FakeKeys]:
    out = io.StringIO()
    keys = FakeKeys(script)
    return RawConsole(io.StringIO(), out, prompt, keys=keys), out, keys


# --- the plain path is unchanged ------------------------------------------------


def test_a_piped_session_gets_the_original_line_buffered_behaviour():
    """Every integration test drives this. It must not have moved."""
    out = io.StringIO()
    console = PlainConsole(io.StringIO("sold mahomes 45 t3\n"), out, "> ")

    console.begin()
    line = console.readline()
    console.notify("recorded")

    assert line == "sold mahomes 45 t3\n"
    assert out.getvalue() == "> recorded\n"


def test_plain_skips_empty_notices_the_way_the_old_writer_did():
    out = io.StringIO()
    PlainConsole(io.StringIO(), out, "> ").notify("")
    assert out.getvalue() == ""


def test_a_non_terminal_never_gets_the_raw_console():
    """StringIO is not a tty, and neither is a redirected log or a pipe."""
    console = make_console(io.StringIO(), io.StringIO(), "> ")
    assert isinstance(console, PlainConsole)


def test_a_console_we_cannot_drive_falls_back_instead_of_failing(monkeypatch):
    """Line editing is a comfort. Recording the draft is not.

    A terminal `_open_keys` cannot take over must cost the user nicer editing,
    never the draft itself.
    """

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def refuse(_stdin):
        raise ConsoleUnavailable("no console here")

    monkeypatch.setattr("ffa.cli.console._open_keys", refuse)
    assert isinstance(make_console(Tty(), Tty(), "> "), PlainConsole)


# --- typing ----------------------------------------------------------------------


def test_a_typed_line_comes_back_terminated_like_a_file_read():
    """The loop treats this exactly like `stdin.readline()`, so it must look like one."""
    console, _, _ = raw("quit\r")
    assert console.readline() == "quit\n"


def test_the_prompt_and_the_keystrokes_are_echoed_by_us_not_the_terminal():
    console, out, _ = raw("hi\r")
    console.readline()
    assert out.getvalue().startswith("> hi")


def test_backspace_removes_the_character_from_the_line_and_the_screen():
    console, out, _ = raw("sole\x7fd\r")
    assert console.readline() == "sold\n"
    assert "\b \b" in out.getvalue()


def test_ctrl_u_clears_a_line_you_want_to_start_over():
    console, _, _ = raw("sold mahmoes\x15sold mahomes\r")
    assert console.readline() == "sold mahomes\n"


def test_an_arrow_key_is_not_typed_into_the_command():
    """`_open_keys` returns "" for a key with no character. It must not land."""
    keys = FakeKeys("so")
    console = RawConsole(io.StringIO(), io.StringIO(), "> ", keys=keys)
    keys.pending += ["", "", "l", "d", "\r"]
    assert console.readline() == "sold\n"


def test_ctrl_c_surfaces_as_the_interrupt_it_is():
    console, _, _ = raw("sold\x03")
    with pytest.raises(KeyboardInterrupt):
        console.readline()


def test_ctrl_d_on_an_empty_line_is_eof():
    console, _, _ = raw("\x04")
    assert console.readline() == ""


def test_ctrl_d_mid_command_does_not_throw_the_command_away():
    """Half a command and a stray Ctrl-D is a slip, not a decision to quit."""
    console, _, _ = raw("sold\x04 mahomes 45 t3\r")
    assert console.readline() == "sold mahomes 45 t3\n"


# --- the actual collision --------------------------------------------------------


def test_a_pick_landing_mid_command_leaves_the_command_intact():
    """The whole point. This is the pick-14 failure, asserted.

    A sale prints while `sold barkl` sits half-typed on the input line, from the
    other thread, exactly as the live loop does it. Afterwards the partial
    command has to be back on screen *and* still submit in full — a redraw that
    looks right but drops the buffer would be worse than the garbling.
    """
    keys = BlockingKeys()
    out = io.StringIO()
    console = RawConsole(io.StringIO(), out, "> ", keys=keys)

    reading = threading.Thread(target=lambda: submitted.append(console.readline()))
    submitted: list[str] = []
    reading.start()

    keys.type("sold barkl")
    _wait_for(out, "> sold barkl")

    console.notify("#7 mahomes -> team 3 for $45")

    screen = out.getvalue()
    assert "#7 mahomes -> team 3 for $45\n" in screen
    assert screen.endswith("> sold barkl")

    keys.type("ey 62 t1\r")
    reading.join(timeout=5)
    assert submitted == ["sold barkley 62 t1\n"]


def test_the_notice_gets_a_clean_line_rather_than_appending_to_the_prompt():
    """Erase first. Otherwise the sale prints after `> sold barkl` and both are lost."""
    console, out, _ = raw()
    console._buffer.extend("sold barkl")
    console._drawn = True

    console.notify("#7 mahomes")

    screen = out.getvalue()
    assert screen.startswith("\r")
    assert screen.index("#7 mahomes") < screen.index("> sold barkl")


def test_a_notice_leaves_a_fresh_prompt_behind_it():
    """The input line is always drawn, so there is always somewhere to type.

    After Enter the previous command belongs to the scrollback, so the notice
    goes out on its own line and an empty prompt follows it — not the finished
    command replayed.
    """
    console, out, _ = raw("quit\r")
    console.readline()
    out.truncate(0)
    out.seek(0)

    console.notify("saved.")

    assert out.getvalue() == "saved.\n> "


def test_the_reader_does_not_draw_a_second_prompt_over_one_notify_left():
    """Two writers, one prompt. Both drawing it is how you get `> > `."""
    keys = BlockingKeys()
    out = io.StringIO()
    console = RawConsole(io.StringIO(), out, "> ", keys=keys)

    console.notify("recorded")
    reading = threading.Thread(target=console.readline, daemon=True)
    reading.start()
    keys.type("q")
    _wait_for(out, "> q")

    assert out.getvalue().count("> ") == 1


def test_notify_stays_quiet_for_empty_text():
    """The loop emits "" as a spacer; redrawing the prompt for it would double it."""
    console, out, _ = raw()
    console._buffer.extend("so")
    console._drawn = True
    out.truncate(0)
    out.seek(0)

    console.notify("")

    assert out.getvalue() == ""


def test_closing_gives_the_terminal_back():
    """On POSIX this is what stops a crash leaving the shell with echo off."""
    console, _, keys = raw()
    console.close()
    assert keys.restored is True
