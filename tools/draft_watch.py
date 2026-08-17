#!/usr/bin/env python3
"""Watch a live ESPN draft land, in a browser, to confirm we read it correctly.

A throwaway diagnostic. It shows two things side by side:

  left   what `SNAPSHOT_JS` actually returned from the draft room
  right  what `parse_snapshot` made of it

and highlights whatever changed since the last poll. The point is to *see* a
pick appear, a budget drop, a bid flash — the things test assertions claim but
cannot show you.

It reads raw and parsed and stops there. No state engine, no journal, no
advisory — so it never opens a run directory and cannot collide with the pid
lockfile a live `ffa draft` session holds. The real draft-day dashboard is
specified in `docs/dashboard_requirements.md` and should be built against that,
not grown out of this.

    python tools/draft_watch.py                     # live, attaches over CDP
    python tools/draft_watch.py --fixture PATH      # offline, no browser

Live mode needs the Chrome started by `tools/draft_room_probe.py --launch`,
with a draft room open in it.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ffa.ingest.espn.draftroom import (  # noqa: E402
    CDP_DEFAULT_PORT,
    DraftRoomError,
    DraftRoomReader,
    RoomSnapshot,
    parse_snapshot,
)

DEFAULT_PORT = 8765


# --- the data side ----------------------------------------------------------


def snapshot_payload(snap: RoomSnapshot) -> dict[str, Any]:
    """The parsed snapshot, as JSON the page can render."""
    return {
        "filled": snap.filled,
        "slots_per_team": snap.slots_per_team,
        "total_cells": snap.slots_per_team * len(snap.teams),
        "pick_header": snap.pick_header,
        "clock": snap.clock,
        "teams": [asdict(t) for t in snap.teams],
        "picks": [
            {**asdict(p), "key": p.key} for p in snap.picks
        ],
        # Surfaced deliberately. ESPN renders "no bid" as the literal string
        # `$null`; the parser turns it into None rather than 0. Showing the
        # count makes it visible that the trap is handled rather than silently
        # producing zeros.
        "null_bids": sum(1 for t in snap.teams if t.bid is None),
        "unpriced_picks": sum(1 for p in snap.picks if p.price is None),
    }


class Watcher:
    """Owns the reader and serialises access to it.

    One reader, one lock: several browser tabs polling at once must not drive
    overlapping CDP evaluates against the same page.
    """

    def __init__(self, *, port: int = CDP_DEFAULT_PORT, fixture: Path | None = None) -> None:
        self._cdp_port = port
        self._fixture = fixture
        self._reader: DraftRoomReader | None = None
        self._lock = threading.Lock()
        self._last_good: dict[str, Any] | None = None
        self._last_good_at: float | None = None

    def read(self) -> dict[str, Any]:
        """Always returns a payload. Never raises — the page must keep rendering."""
        with self._lock:
            try:
                raw = self._read_raw()
            except Exception as exc:  # noqa: BLE001 - degrade, never die
                return self._degraded(exc)

            payload = {
                "ok": True,
                "error": None,
                "stale_for": None,
                "at": time.time(),
                "mode": "fixture" if self._fixture else "live",
                "raw": raw,
                "parsed": snapshot_payload(parse_snapshot(raw)),
            }
            self._last_good = payload
            self._last_good_at = payload["at"]
            return payload

    def _read_raw(self) -> dict[str, Any]:
        if self._fixture is not None:
            return json.loads(self._fixture.read_text(encoding="utf-8"))
        if self._reader is None:
            self._reader = DraftRoomReader(port=self._cdp_port).connect()
        return self._reader.snapshot_raw()

    def _degraded(self, exc: Exception) -> dict[str, Any]:
        """Keep the last good data on screen rather than blanking the page.

        A blank page during a draft is indistinguishable from "nothing has
        happened", which is exactly the confusion this tool exists to remove.
        """
        # Drop the reader so the next poll reconnects — the tab may have been
        # closed and reopened, which a cached page handle would never recover
        # from.
        self._reader = None

        message = str(exc) if isinstance(exc, DraftRoomError) else f"{type(exc).__name__}: {exc}"
        stale_for = (time.time() - self._last_good_at) if self._last_good_at else None
        base = dict(self._last_good) if self._last_good else {"raw": None, "parsed": None}
        base.update({
            "ok": False,
            "error": message,
            "stale_for": round(stale_for, 1) if stale_for is not None else None,
            "at": time.time(),
            "mode": "fixture" if self._fixture else "live",
        })
        return base

    def close(self) -> None:
        with self._lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None


# --- the page ---------------------------------------------------------------

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>draft watch</title>
<style>
  :root {
    --bg:#11131a; --panel:#181b24; --line:#262b38; --ink:#dfe3ee; --dim:#8b93a7;
    --ok:#5ad18f; --warn:#f0b849; --bad:#ff6b6b; --hit:#f5c451; --me:#6aa9ff;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  header { display:flex; gap:16px; align-items:baseline; flex-wrap:wrap;
           padding:10px 14px; border-bottom:1px solid var(--line); position:sticky; top:0;
           background:var(--bg); z-index:5; }
  h1 { font-size:14px; margin:0; font-weight:600; letter-spacing:.02em; }
  .pill { padding:1px 8px; border:1px solid var(--line); border-radius:99px; color:var(--dim); }
  .pill.ok { color:var(--ok); border-color:#265; }
  .pill.bad { color:var(--bad); border-color:#633; }
  main { display:grid; grid-template-columns:minmax(320px,1fr) minmax(420px,1.4fr);
         gap:14px; padding:14px; align-items:start; }
  section { background:var(--panel); border:1px solid var(--line); border-radius:8px; }
  h2 { font-size:11px; text-transform:uppercase; letter-spacing:.09em; color:var(--dim);
       margin:0; padding:9px 12px; border-bottom:1px solid var(--line); }
  pre { margin:0; padding:12px; overflow:auto; max-height:78vh; color:#b9c2d6;
        font-size:12px; white-space:pre-wrap; word-break:break-word; }
  table { width:100%; border-collapse:collapse; }
  th,td { padding:4px 8px; text-align:left; border-bottom:1px solid #1e222d; }
  th { color:var(--dim); font-weight:500; font-size:11px; text-transform:uppercase;
       letter-spacing:.06em; position:sticky; top:0; background:var(--panel); }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .scroll { max-height:44vh; overflow:auto; }
  .dim { color:var(--dim); }
  .me { color:var(--me); }
  .flag { font-size:10px; padding:0 5px; border-radius:3px; border:1px solid var(--line);
          color:var(--dim); margin-right:3px; }
  .stats { display:flex; gap:18px; flex-wrap:wrap; padding:10px 12px; }
  .stat b { display:block; font-size:19px; font-weight:600; }
  .stat span { color:var(--dim); font-size:11px; }
  .banner { padding:9px 12px; background:#3a2020; color:#ffb3b3;
            border-bottom:1px solid #633; }
  @keyframes hit { from { background:#4a3c12; } to { background:transparent; } }
  .changed { animation:hit 1.6s ease-out; }
  .stale { opacity:.45; }
</style>

<header>
  <h1>draft watch</h1>
  <span id="mode" class="pill">…</span>
  <span id="status" class="pill">connecting</span>
  <span id="board" class="pill"></span>
  <span id="nulls" class="pill" title="ESPN renders 'no bid' as the string $null. These parse to None, never 0."></span>
  <span id="polls" class="pill"></span>
</header>

<div id="banner" class="banner" style="display:none"></div>

<main>
  <section>
    <h2>raw — what SNAPSHOT_JS returned</h2>
    <pre id="raw">…</pre>
  </section>

  <section>
    <h2>parsed — RoomSnapshot</h2>
    <div class="stats" id="stats"></div>
    <h2>teams</h2>
    <div class="scroll"><table id="teams"></table></div>
    <h2>picks</h2>
    <div class="scroll"><table id="picks"></table></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
let prev = {};          // signature per row key, for change highlighting
let polls = 0;

const money = (v) => v === null || v === undefined
  ? '<span class="dim">—</span>'
  : '$' + v;

function rowChanged(key, sig) {
  const changed = prev[key] !== undefined && prev[key] !== sig;
  prev[key] = sig;
  return changed;
}

function renderTeams(teams) {
  let html = '<tr><th>#</th><th>team</th><th class="num">cash</th>'
           + '<th class="num">bid</th><th>flags</th></tr>';
  for (const t of teams) {
    const sig = JSON.stringify([t.cash, t.bid, t.is_auto, t.is_nominating]);
    const hit = rowChanged('team:' + t.index, sig) ? ' class="changed"' : '';
    const flags = [
      t.is_me ? '<span class="flag me">me</span>' : '',
      t.is_auto ? '<span class="flag">auto</span>' : '',
      t.is_nominating ? '<span class="flag">nominating</span>' : '',
    ].join('');
    html += `<tr${hit}><td class="dim">${t.index}</td>`
         +  `<td${t.is_me ? ' class="me"' : ''}>${esc(t.name)}</td>`
         +  `<td class="num">${money(t.cash)}</td>`
         +  `<td class="num">${money(t.bid)}</td><td>${flags}</td></tr>`;
  }
  $('teams').innerHTML = html;
}

function renderPicks(picks) {
  let html = '<tr><th class="num">grid</th><th class="num">team</th><th>player</th>'
           + '<th class="num">price</th><th>pos</th><th>slot</th></tr>';
  // Newest first: a pick landing should appear at the top, where you are looking.
  for (const p of [...picks].reverse()) {
    const sig = JSON.stringify([p.price, p.team_index, p.position]);
    const hit = rowChanged('pick:' + p.key, sig) ? ' class="changed"' : '';
    html += `<tr${hit}><td class="num dim">${p.grid_index}</td>`
         +  `<td class="num">${p.team_index}</td>`
         +  `<td${p.is_mine ? ' class="me"' : ''}>${esc(p.player)}</td>`
         +  `<td class="num">${money(p.price)}</td>`
         +  `<td class="dim">${esc(p.position ?? '')}</td>`
         +  `<td class="dim">${esc(p.roster_slot ?? '')}</td></tr>`;
  }
  $('picks').innerHTML = html;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]
  ));
}

function renderStats(p) {
  $('stats').innerHTML = `
    <div class="stat"><b>${p.filled}</b><span>filled / ${p.total_cells}</span></div>
    <div class="stat"><b>${p.slots_per_team}</b><span>slots per team (derived)</span></div>
    <div class="stat"><b>${p.teams.length}</b><span>teams</span></div>
    <div class="stat"><b>${esc(p.pick_header ?? '—')}</b><span>pick header</span></div>
    <div class="stat"><b>${esc(p.clock ?? '—')}</b><span>clock</span></div>`;
}

async function tick() {
  try {
    const r = await fetch('/data', {cache: 'no-store'});
    const d = await r.json();
    polls++;

    $('mode').textContent = d.mode;
    $('polls').textContent = polls + ' polls';
    document.body.classList.toggle('stale', !d.ok);

    if (d.ok) {
      $('status').textContent = 'ok';
      $('status').className = 'pill ok';
      $('banner').style.display = 'none';
    } else {
      $('status').textContent = d.stale_for ? `stale ${d.stale_for}s` : 'error';
      $('status').className = 'pill bad';
      $('banner').style.display = '';
      $('banner').textContent = d.error;
    }

    if (d.parsed) {
      const p = d.parsed;
      $('board').textContent = `${p.filled}/${p.total_cells} picks`;
      $('nulls').textContent = `${p.null_bids} $null bids · ${p.unpriced_picks} unpriced`;
      renderStats(p);
      renderTeams(p.teams);
      renderPicks(p.picks);
    }
    if (d.raw) $('raw').textContent = JSON.stringify(d.raw, null, 2);
  } catch (e) {
    $('status').textContent = 'server unreachable';
    $('status').className = 'pill bad';
  }
}

tick();
setInterval(tick, POLL_MS);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    watcher: Watcher
    poll_ms: int = 1000

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.startswith("/data"):
            body = json.dumps(self.watcher.read()).encode("utf-8")
            self._send(body, "application/json")
        elif self.path in ("/", "/index.html"):
            page = PAGE.replace("POLL_MS", str(self.poll_ms))
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Silence per-request logging — it polls once a second."""


def serve(watcher: Watcher, port: int, poll_ms: int, *, open_browser: bool = True) -> None:
    handler = type("BoundHandler", (Handler,), {"watcher": watcher, "poll_ms": poll_ms})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"draft watch on {url}  (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
        watcher.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to serve on")
    parser.add_argument("--cdp-port", type=int, default=CDP_DEFAULT_PORT,
                        help="the Chrome remote-debugging port to attach to")
    parser.add_argument("--fixture", type=Path, default=None,
                        help="render a captured snapshot instead of attaching to Chrome")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between polls (default 1.0)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser")
    args = parser.parse_args()

    if args.fixture is not None and not args.fixture.is_file():
        print(f"error: no such fixture: {args.fixture}", file=sys.stderr)
        return 1

    watcher = Watcher(port=args.cdp_port, fixture=args.fixture)
    serve(watcher, args.port, int(args.interval * 1000), open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
