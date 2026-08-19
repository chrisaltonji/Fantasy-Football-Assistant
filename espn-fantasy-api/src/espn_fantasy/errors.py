"""One error type, so callers have one thing to catch.

Every message here is written to be printed straight at a user without a
traceback: what failed, and what to do about it. ESPN's failure modes are
mostly indistinguishable from each other at the HTTP layer — a private league
with no cookies 404s rather than 401s — so the text carries the disambiguation
the status code doesn't.
"""

from __future__ import annotations


class EspnApiError(Exception):
    """A failure the caller can act on."""
