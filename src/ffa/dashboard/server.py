"""A read-only window onto a running draft.

The dashboard is display-only, and this is what enforces that. It never opens a
`DraftStore`, never takes the pid lock, and never dispatches an event — it
replays the journal the REPL is writing and serves the same `build_view()`
payload every other surface reads. The single-writer discipline the whole engine
rests on is preserved by construction rather than by convention.

**Why a server at all.** A page opened over `file://` cannot fetch a sibling
JSON file — Chrome blocks it — so a static dashboard could never refresh itself.
Serving the page and the state from one local origin is the smallest thing that
works, needs no build step, and keeps the payload identical to `ffa
export-state`.

**Reading a file that is being appended to is safe here, and not by luck.** The
journal is append-only JSONL, fsynced before each prompt returns, and
`read_events` already treats a torn trailing line as the one casualty of a
crash: it drops it and carries on. A concurrent reader hits exactly that case,
so the tolerance that exists for crash-resume is the same tolerance that makes
this sound.

Binds to loopback only. This puts your league's rosters and every manager's
budget on an HTTP port; that is for the machine you are drafting on, not the
network you are drafting from.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PAGE = Path(__file__).with_name("page.html")


class StateReader:
    """Replays the journal on demand, and only when it has actually changed.

    The advisory layer costs ~40 ms over a full board, which is nothing against
    a 30-second nomination clock but is pure waste at one poll per second on an
    idle draft. Size plus mtime is enough to notice an append: the journal only
    ever grows, and every write goes through one process.
    """

    def __init__(self, run_dir: Path, *, config=None, book=None,
                 dossiers=None, precedent=None, seats=None) -> None:
        self.run_dir = run_dir
        self._config = config
        self._book = book
        self._dossiers = dossiers
        self._precedent = precedent
        self._seats = seats
        self._lock = threading.Lock()
        self._stamp: tuple[int, float] | None = None
        self._assist_stamp: tuple[int, float] | None = None
        self._cached: dict[str, Any] | None = None
        self.warnings: list[str] = []

    @property
    def journal(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def sidecar(self) -> Path:
        """The assistant's ledger. Read-only, and optional in every sense.

        This is the closest the dashboard gets to inference: it reads what the
        agents already said and renders it. It makes **no API calls** — a second
        process holding a key and spending money behind a browser tab is a
        different tool from a read-only board, and this stays the read-only one.
        """
        return self.run_dir / "assist.jsonl"

    def _stat(self, path: Path) -> tuple[int, float] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime)

    def _fingerprint(self) -> tuple[int, float] | None:
        return self._stat(self.journal)

    def view(self) -> dict[str, Any]:
        from ffa.domain.reducers import replay
        from ffa.state.journal import read_events
        from ffa.view.model import build_view

        with self._lock:
            stamp = self._fingerprint()
            # Two files, two clocks. The sidecar grows on its own schedule — a
            # read lands seconds after the pick that triggered it — so keying
            # the cache on the journal alone would leave every read invisible
            # until the *next* pick happened to invalidate it.
            assist_stamp = self._stat(self.sidecar)
            if (stamp is not None and stamp == self._stamp
                    and assist_stamp == self._assist_stamp
                    and self._cached is not None):
                return self._cached

            if stamp is None:
                # No journal yet. Say so in the payload's own vocabulary rather
                # than erroring: "not started" is a state the page must render.
                return {"schema_version": 1, "initialized": False,
                        "draft_id": self.run_dir.name,
                        "warnings": [f"no journal at {self.journal}"]}

            events, warnings = read_events(self.journal)
            self.warnings = list(warnings)
            state = replay(events, self.run_dir.name)
            view = build_view(
                state, self._book,
                dossiers=self._dossiers,
                precedent=self._precedent,
                seats=self._seats,
                strategy=getattr(self._config, "strategy", None),
                # The feed is a fold over the journal, and this is the one
                # caller that has already read it. Cached with the rest of the
                # view by journal fingerprint, so a 180-pick fold runs once per
                # pick rather than once per poll.
                events=events,
                assist=self._assist_view(state),
            )
            if warnings:
                view.setdefault("warnings", []).extend(warnings)

            self._stamp = stamp
            self._assist_stamp = assist_stamp
            self._cached = view
            return view

    def _assist_view(self, state) -> dict:
        """Whatever the agents said, or `{}`.

        Swallows everything on purpose. A corrupt sidecar, a missing module, an
        `assist/` package that was never installed — none of it is a reason for
        the board to stop rendering. The dashboard's job is the arithmetic, and
        the arithmetic does not depend on any of this.
        """
        try:
            from ffa.assist.context import ReadLog
            from ffa.assist.view import assist_view

            log = ReadLog.load(self.sidecar)
            if not log.all():
                return {}
            # `nomination.ref.key`, the same path view/model.py uses. Reaching
            # for a `player_key` attribute that does not exist would return ""
            # forever and silently show no current read at all.
            nomination = getattr(state, "current_nomination", None)
            key = getattr(getattr(nomination, "ref", None), "key", "") or ""
            return assist_view(log, player_key=key)
        except Exception:  # noqa: BLE001 - never let inference break the board
            return {}


def make_handler(reader: StateReader, interval: float):
    class Handler(BaseHTTPRequestHandler):
        # Quiet by default. A request log line per poll would bury the REPL
        # output you are actually drafting against.
        def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError):
                # The tab closed mid-write. Not an error worth surfacing while
                # a draft is running.
                pass

        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            path = self.path.split("?", 1)[0]

            if path in ("/", "/index.html"):
                try:
                    body = PAGE.read_bytes()
                except OSError as exc:
                    self._send(500, f"cannot read {PAGE}: {exc}".encode(), "text/plain")
                    return
                self._send(200, body, "text/html; charset=utf-8")
                return

            if path == "/state.json":
                try:
                    payload = reader.view()
                except Exception as exc:  # noqa: BLE001 - surfaced, never fatal
                    # A read that fails must not take the page down: it renders
                    # the error and keeps polling, because the next poll may
                    # well succeed and a blank dashboard mid-draft is worse.
                    payload = {"initialized": False,
                               "warnings": [f"{type(exc).__name__}: {exc}"]}
                body = json.dumps(payload).encode("utf-8")
                self._send(200, body, "application/json")
                return

            if path == "/meta.json":
                body = json.dumps({
                    "poll_interval": interval,
                    "run_dir": str(reader.run_dir),
                }).encode("utf-8")
                self._send(200, body, "application/json")
                return

            self._send(404, b"not found", "text/plain")

    return Handler


def serve(reader: StateReader, *, host: str = "127.0.0.1", port: int = 8765,
          interval: float = 2.0) -> ThreadingHTTPServer:
    """Start the server. Caller owns shutdown."""
    server = ThreadingHTTPServer((host, port), make_handler(reader, interval))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
