"""Who owns the bottom line of the terminal.

The live draft loop has two things writing to one screen: you, typing a
correction, and the ESPN reader, announcing a pick that just landed. Under the
terminal's own line discipline they collide. The tty echoes your keystrokes
wherever the cursor happens to be, so a pick printed mid-word splits the command
across two lines — the characters are still in the kernel's input buffer, but
the prompt they belong to has scrolled away. What you see and what you are about
to submit stop agreeing. It was hit at pick 14 of the rehearsal, which is a bad
moment to discover you cannot trust the screen.

The fix is to take the echo away from the terminal. `RawConsole` reads one key
at a time with echo off and prints the characters itself, which means it always
knows exactly what is on the input line — so before printing a pick it can wipe
that line, print, and put your half-typed command back underneath, intact.

Two deliberate limits:

- **Only when both ends are a terminal.** A piped transcript has no echo to
  fight and no cursor to move; every integration test drives that path, and it
  keeps the exact byte-for-byte behaviour it had before this module existed.
  `PlainConsole` is that path.
- **No ANSI.** Erasing is spaces and carriage returns. Windows consoles need
  virtual-terminal processing switched on before escape codes mean anything,
  and a draft is the wrong place to find out this one was not.
"""

from __future__ import annotations

import shutil
import sys
from typing import TextIO


class ConsoleUnavailable(Exception):
    """Raw key reading is not possible here. Fall back, never fail."""


# --- the plain path -----------------------------------------------------------


class PlainConsole:
    """Line-buffered reads and unadorned writes: the original behaviour.

    Used whenever stdin or stdout is not a terminal — a piped session
    transcript, a test, a redirected log. Nothing is echoed by us and nothing is
    redrawn, because there is no cursor to fight over.
    """

    interactive = False

    def __init__(self, stdin: TextIO, stdout: TextIO, prompt: str) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._prompt = prompt

    def begin(self) -> None:
        """Draw the prompt for the read that is about to happen."""
        self._stdout.write(self._prompt)
        self._stdout.flush()

    def notify(self, text: str) -> None:
        if text:
            self._stdout.write(text + "\n")
            self._stdout.flush()

    def readline(self) -> str:
        return self._stdin.readline()

    def close(self) -> None:
        return None


# --- reading one key at a time -------------------------------------------------


class _WindowsKeys:
    """`msvcrt` reads a key without echoing it and without touching tty modes.

    Nothing needs restoring afterwards, which also means a crash or a Ctrl-C
    cannot leave the console in a strange state — the failure mode POSIX raw
    mode has to be careful about.
    """

    def __init__(self) -> None:
        import msvcrt  # noqa: F401 - presence is the check

        self._msvcrt = msvcrt

    def read(self) -> str:
        ch = self._msvcrt.getwch()
        # Arrows, function keys and the rest arrive as a two-character sequence.
        # Swallow the second half, or it lands in the buffer as a stray letter.
        if ch in ("\x00", "\xe0"):
            self._msvcrt.getwch()
            return ""
        return ch

    def restore(self) -> None:
        return None


class _PosixKeys:
    """cbreak mode on the tty, restored on the way out."""

    def __init__(self, fd: int) -> None:
        import termios
        import tty

        self._termios = termios
        self._fd = fd
        self._saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)

    def read(self) -> str:
        import os
        import select

        data = os.read(self._fd, 1)
        if not data:
            return "\x04"  # a closed stdin is an EOF, same as Ctrl-D
        ch = data.decode("utf-8", "replace")
        if ch == "\x1b":
            # An escape sequence: drain whatever arrived with it rather than
            # letting "[A" from an arrow key be typed into the command.
            while select.select([self._fd], [], [], 0)[0]:
                if not os.read(self._fd, 1):
                    break
            return ""
        return ch

    def restore(self) -> None:
        self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._saved)


def _open_keys(stdin: TextIO):
    if sys.platform == "win32":
        try:
            return _WindowsKeys()
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            raise ConsoleUnavailable(str(exc)) from exc
    try:
        return _PosixKeys(stdin.fileno())
    except Exception as exc:  # noqa: BLE001
        raise ConsoleUnavailable(str(exc)) from exc


# --- the interactive path -------------------------------------------------------

ENTER = ("\r", "\n")
BACKSPACE = ("\x7f", "\x08")
CTRL_C = "\x03"
CTRL_D = "\x04"
CTRL_U = "\x15"


class RawConsole:
    """Owns the prompt, the line being typed, and every write that interrupts it.

    `readline` runs on the stdin pump thread and `notify` on the main loop, so
    every screen operation takes the same lock. Without it the two interleave at
    exactly the moment the tool is most needed — a pick landing on the keystroke
    you are typing is not a rare case during an auction, it is the normal one.
    """

    interactive = True

    def __init__(
        self, stdin: TextIO, stdout: TextIO, prompt: str, keys=None
    ) -> None:
        import threading

        self._stdout = stdout
        self._prompt = prompt
        # `keys` is the one seam here. Raw key reading needs a real console on
        # both platforms, so without it the interesting half of this class —
        # what the screen looks like when a pick interrupts a half-typed
        # command — could only be checked by hand, on draft day.
        self._keys = keys if keys is not None else _open_keys(stdin)
        self._lock = threading.RLock()
        self._buffer: list[str] = []
        self._drawn = False

    # -- screen ---------------------------------------------------------------

    def _width(self) -> int:
        try:
            return max(shutil.get_terminal_size().columns, 20)
        except OSError:  # pragma: no cover - no terminal to size
            return 80

    def _erase(self) -> None:
        """Blank the input line without assuming ANSI is available."""
        if not self._drawn:
            return
        span = min(len(self._prompt) + len(self._buffer), self._width() - 1)
        self._stdout.write("\r" + " " * span + "\r")
        self._drawn = False

    def _draw(self) -> None:
        self._stdout.write(self._prompt + "".join(self._buffer))
        self._drawn = True

    def begin(self) -> None:
        """No-op: the reader thread draws and keeps the prompt.

        The main loop asks for a prompt once per iteration, which is right for
        the plain path. Here a second writer would double it.
        """
        return None

    def notify(self, text: str) -> None:
        """Print above the line being typed, then put that line back."""
        if not text:
            return
        with self._lock:
            self._erase()
            self._stdout.write(text + "\n")
            self._draw()
            self._stdout.flush()

    # -- input ----------------------------------------------------------------

    def readline(self) -> str:
        """One command, echoed by us. `""` means EOF, as with a file."""
        with self._lock:
            if not self._drawn:
                self._draw()
                self._stdout.flush()

        while True:
            ch = self._keys.read()
            if ch == "":
                continue

            if ch in ENTER:
                with self._lock:
                    line = "".join(self._buffer)
                    self._buffer.clear()
                    self._stdout.write("\n")
                    self._drawn = False
                    self._stdout.flush()
                return line + "\n"

            if ch in BACKSPACE:
                with self._lock:
                    if self._buffer:
                        self._buffer.pop()
                        # Back over the character, blank it, back up again.
                        self._stdout.write("\b \b")
                        self._stdout.flush()
                continue

            if ch == CTRL_U:
                with self._lock:
                    self._buffer.clear()
                    self._erase()
                    self._draw()
                    self._stdout.flush()
                continue

            if ch == CTRL_C:
                raise KeyboardInterrupt

            if ch == CTRL_D:
                with self._lock:
                    if self._buffer:
                        continue
                    self._stdout.write("\n")
                    self._drawn = False
                    self._stdout.flush()
                return ""

            if ch < " ":  # any other control character
                continue

            with self._lock:
                self._buffer.append(ch)
                self._stdout.write(ch)
                self._drawn = True
                self._stdout.flush()

    def close(self) -> None:
        with self._lock:
            self._erase()
            self._stdout.flush()
        self._keys.restore()


# --- choosing one ---------------------------------------------------------------


def _is_terminal(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a stream that cannot say is not one
        return False


def make_console(stdin: TextIO, stdout: TextIO, prompt: str):
    """`RawConsole` when there is a real terminal on both ends, else `PlainConsole`.

    Falling back is never an error. Line editing is a comfort; recording the
    draft is not, and a console this cannot drive must not stop a draft.
    """
    if _is_terminal(stdin) and _is_terminal(stdout):
        try:
            return RawConsole(stdin, stdout, prompt)
        except ConsoleUnavailable:
            pass
    return PlainConsole(stdin, stdout, prompt)
