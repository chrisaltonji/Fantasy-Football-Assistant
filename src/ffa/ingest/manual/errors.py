"""Shared error type for the manual input path."""

from __future__ import annotations


class CommandError(Exception):
    """Something the user typed that we won't act on.

    Mirrors the `ConfigError` precedent from CP1: the message goes straight to
    the user as `error: ...`, never a traceback, and it should say what is
    wrong *and* what to do instead.

    Raising this is the only way to refuse input. Once an event is appended to
    the journal it is history, and reducers may not second-guess it — so every
    rejection has to happen here, before anything is written.
    """
