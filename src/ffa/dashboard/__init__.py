"""The read-only dashboard surface.

Display-only by construction: it replays the journal and serves `build_view()`,
never opening a store and never taking the write lock. See `server.py`.
"""

from ffa.dashboard.server import StateReader, serve

__all__ = ["StateReader", "serve"]
