"""The one exception type this package lets out.

Everything here can fail in ways the draft must survive: no network, no key, a
rate limit, a schema the API rejects, a reply that parses but says something the
guard refuses. All of it becomes `AssistError` with a message written for a
person, because the alternative is a library traceback surfacing mid-auction.

Deliberately *not* a `ConfigError`. That type means "you must fix this before the
tool works", and `main()` prints it and exits 2. Nothing in here is ever fatal —
the draft runs identically without an assistant, so an assist failure is a muted
panel and a line of explanation, never an exit code.
"""

from __future__ import annotations


class AssistError(Exception):
    """A failure in the inference layer. Never fatal, always explained."""

    def __init__(self, message: str, *, fatal_to_assist: bool = False,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        # True for the failures where retrying 180 times is pure waste — a bad
        # key, a schema we got wrong. The runner mutes immediately rather than
        # spending the draft rediscovering it.
        self.fatal_to_assist = fatal_to_assist
        self.retry_after = retry_after
