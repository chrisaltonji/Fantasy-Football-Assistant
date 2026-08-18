"""The draft-history report: one self-contained HTML file.

HTML rather than Markdown by the project's own scoring — human audience, written
once per offseason, and the whole point is comparing distributions, which is a
picture. No build step, no CDN, opens by double-clicking.

Two things the design is answerable for:

- **Provenance first, before any finding.** The reference price comes from a
  different ESPN field in different seasons and reaches between 46% and 82% of
  picks. A reader who does not know that will read a 2022 number as though it
  were a 2025 number, so the coverage table sits above the analysis rather than
  in a footnote.
- **Every label shows its own evidence.** A classification here is a statement
  about *rank within this league*, not about a manager in the abstract, so the
  number that produced it is printed beside it and the league average sits
  behind it as a tick. A thin margin has to look thin.

Colours follow the validated categorical palette (blue / orange / aqua, which
clears CVD separation on all pairs in both modes). Every bar carries a visible
number, which is also what discharges the light-mode contrast relief rule.
"""

from __future__ import annotations

import html
from typing import Sequence

from ffa.history.metrics import ManagerProfile, ManagerSeason
from ffa.history.schema import DraftPick, History

SHAPE_WORDS = {
    "stars_and_scrubs": "stars & scrubs",
    "balanced": "balanced",
    "value_hunter": "value hunter",
}
PACE_WORDS = {
    "front_loads": "front-loads",
    "steady": "steady",
    "waits": "waits",
}
CHASE_WORDS = {
    "often": "chases often",
    "sometimes": "chases sometimes",
    "rarely": "disciplined",
}

NOM_WORDS = {
    "nominates_high": "nominates high",
    "nominates_low": "nominates low",
    "no_pattern": "no pattern",
}

# One place that turns any machine label into English, so a raw `value_hunter`
# can never reach the page through a path somebody forgot to route.
WORDS = {**SHAPE_WORDS, **PACE_WORDS, **CHASE_WORDS, **NOM_WORDS}


def say(label: str) -> str:
    return WORDS.get(label, label.replace("_", " ") if label else "")


def say_agreement(summary: str) -> str:
    """`3/4 value_hunter` -> `3/4 seasons: value hunter`."""
    if "/" not in summary:
        return summary
    count, _, label = summary.partition(" ")
    return f"{count} seasons: {say(label)}" if label else summary

CSS = """
:root{color-scheme:light;
 --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
 --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
 --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
 --pos:#256abf; --neg:#d03b3b; --good:#0ca30c; --warn:#fab219;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
 --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
 --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
 --s1:#3987e5; --s2:#d95926; --s3:#199e70;
 --pos:#3987e5; --neg:#e66767;}}
*{box-sizing:border-box;min-width:0}
html,body{overflow-x:clip;max-width:100%}
body{margin:0;background:var(--plane);color:var(--ink);
 font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:30px;margin:0 0 6px;letter-spacing:-.02em}
h2{font-size:20px;margin:40px 0 14px;letter-spacing:-.01em}
h3{font-size:17px;margin:0 0 2px}
.sub{color:var(--ink2);margin:0 0 26px;max-width:70ch}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
 padding:20px 22px;margin:14px 0}
.grid{display:grid;gap:14px}
@media(min-width:820px){.grid.two{grid-template-columns:1fr 1fr}}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:6px 10px 6px 0;border-bottom:1px solid var(--grid)}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;
 letter-spacing:.04em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
/* `nowrap` keeps a chip on one line where there is room. Below the wide
   breakpoint the evidence string is longer than the viewport, so it has to be
   allowed to wrap — a chip is not worth a horizontally scrolling page. */
.chip{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12.5px;
 font-weight:600;border:1px solid var(--border);margin:0 6px 6px 0;white-space:nowrap;
 max-width:100%}
@media(max-width:760px){.chip{white-space:normal;border-radius:10px}}
.chip b{font-weight:700}
.chip .ev{color:var(--ink2);font-weight:500}
.bar{position:relative;height:10px;background:var(--grid);border-radius:5px;overflow:hidden}
.bar>span{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:var(--s1)}
.row{display:grid;grid-template-columns:44px 1fr 104px;gap:10px;align-items:center;
 margin:5px 0;font-size:13px}
.row .val{white-space:nowrap}
.row .lab{color:var(--ink2)}
.row .val{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink2)}
.stack{display:flex;height:22px;border-radius:6px;overflow:hidden;background:var(--grid)}
.stack i{display:block;font-style:normal;font-size:11px;color:#fff;text-align:center;
 line-height:22px;border-right:2px solid var(--surface)}
.stack i:last-child{border-right:0}
.tick{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink2);opacity:.75}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--ink2);margin:8px 0 0}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:10px;height:10px;border-radius:3px;display:inline-block}
.kv{display:flex;gap:28px;flex-wrap:wrap;margin:2px 0 14px;font-size:13px;color:var(--ink2)}
.kv b{color:var(--ink);font-variant-numeric:tabular-nums}
.note{font-size:12.5px;color:var(--muted);margin:8px 0 0}
.warnbox{border-left:3px solid var(--warn);padding:2px 0 2px 12px;margin:10px 0;
 font-size:13px;color:var(--ink2)}
details{margin-top:12px}
summary{cursor:pointer;color:var(--ink2);font-size:13px}
.pos{color:var(--neg)}   /* paid over reference */
.neg{color:var(--good)}  /* paid under reference */
.gap td{color:var(--muted)}
"""


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _bar(value: float, cap: float, label: str, tick: float | None = None,
         color: str = "var(--s1)") -> str:
    width = max(0.0, min(1.0, value / cap if cap else 0.0)) * 100
    marker = ""
    if tick is not None and cap:
        left = max(0.0, min(1.0, tick / cap)) * 100
        marker = f'<i class="tick" style="left:{left:.1f}%"></i>'
    return (
        f'<div class="bar"><span style="width:{width:.1f}%;background:{color}"></span>'
        f"{marker}</div><span class=val>{_e(label)}</span>"
    )


def _chip(word: str, evidence: str, color: str) -> str:
    if not word:
        return (
            '<span class="chip"><b>no read</b> '
            f'<span class=ev>{_e(evidence)}</span></span>'
        )
    return (
        f'<span class="chip" style="border-color:{color}">'
        f"<b>{_e(word)}</b> <span class=ev>{_e(evidence)}</span></span>"
    )


# --- sections ---------------------------------------------------------------------


def _provenance(history: History) -> str:
    rows = "".join(
        f"<tr><td><b>{s.year}</b></td><td class=n>{len(s.picks)}</td>"
        f"<td class=n>${s.total_spend:,}</td>"
        f"<td class=n>{'yes' if s.has_nominations else '<b>no</b>'}</td>"
        f"<td class=n>{_pct(s.reference_coverage)}</td>"
        f"<td>{_e(s.reference_source)}</td></tr>"
        for s in history.seasons
    )
    gaps = "".join(
        f'<tr class=gap><td><b>{g.year}</b></td><td colspan=5>{_e(g.reason)}</td></tr>'
        for g in sorted(history.gaps, key=lambda g: g.year)
    )
    return f"""
<h2>Where this data came from</h2>
<p class=sub>Every number below rests on this table. The price reference is
<b>not the same ESPN field in every season</b> and reaches between
{_pct(min(s.reference_coverage for s in history.seasons))} and
{_pct(max(s.reference_coverage for s in history.seasons))} of picks, so an
over-pay rate from 2022 is a weaker claim than the same rate from 2025.</p>
<div class="card scroll"><table>
<tr><th>Season</th><th class=n>Picks</th><th class=n>Spent</th>
<th class=n>Nominations</th><th class=n>Price ref. coverage</th>
<th>Reference source</th></tr>
{rows}{gaps}</table>
<p class=note>Seasons in grey were not usable. They are listed rather than
dropped: &ldquo;no data for 2021&rdquo; and &ldquo;2021 was a ten-team offline
draft&rdquo; support very different conclusions, and only one of them is true.</p>
</div>"""


EVIDENCE_WORDS = {
    "pace": "Pace — share of budget spent by the 30% mark",
    "spend_shape": "Spend shape — top-3 buys as a share of budget",
    "te_share": "TE allocation",
    "rb_share": "RB allocation",
    "wr_share": "WR allocation",
    "qb_share": "QB allocation",
    "chasing": "Chasing — paid vs the reference price",
    "nomination_premium": "Nomination premium — $ over the going rate",
    "self_win_rate": "Self-win rate — winning your own nominations",
}


def _evidence(scores) -> str:
    """What was tested, and what did not survive.

    This section exists because the first version of this report shipped four
    findings that were noise, and one of them was about the reader. The numbers
    are recomputed on every run rather than quoted from a comment, so the claim
    cannot go stale while still sounding authoritative.
    """
    if not scores:
        return ""

    rows = ""
    for name, ev in sorted(scores.items(), key=lambda kv: -kv[1].z):
        colour = "var(--good)" if ev.survives else "var(--muted)"
        weight = "600" if ev.survives else "400"
        rows += (
            f"<tr><td style=\"font-weight:{weight}\">"
            f"{_e(EVIDENCE_WORDS.get(name, name))}</td>"
            f"<td class=n>{_e(ev.null)}</td>"
            f"<td class=n>{ev.observed:.4f}</td><td class=n>{ev.mean:.4f}</td>"
            f"<td class=n style=\"color:{colour};font-weight:{weight}\">"
            f"{ev.z:+.1f}</td>"
            f"<td style=\"color:{colour}\">{_e(ev.verdict)}</td></tr>"
        )

    survivors = [n for n, e in scores.items() if e.survives]
    rounds = next(iter(scores.values())).rounds

    return f"""
<h2>What was tested, and what did not survive</h2>
<p class=sub>Four seasons of twelve managers is about fifteen picks each &mdash;
enough to rank people, nowhere near enough to stop a plausible pattern appearing
on its own. So every claim is scored against a null: shuffle the thing that is
supposed to carry the signal {rounds} times and ask whether the real spread is
wider than the shuffled one. <b>{len(survivors)} of {len(scores)} survived.</b></p>
<div class="card scroll"><table>
<tr><th>Signal</th><th class=n>Null</th><th class=n>Observed</th>
<th class=n>Chance</th><th class=n>z</th><th>Verdict</th></tr>
{rows}</table>
<p class=note><b>The null has to respect what generated the data.</b> Shuffling
individual picks is right for NFL-team affinity or who nominated what, and wrong
for anything the $200 and the fifteen slots constrain &mdash; it lets an
imaginary manager hold three $70 players. Under that null, pace scored
<b>z = &minus;1.7</b>: a spread <em>narrower</em> than random, which a real
effect cannot produce and which is the signature of a broken test rather than a
finding. Swapping whole rosters instead moved it to +3.4.</p>
<p class=note><b>Homer teams and repeat buys are gone from this report.</b> Both
were tested at pick level, where the shuffle is valid, and neither is
distinguishable from chance (z = &minus;0.3 and +1.2). Thirty-two NFL teams
across twelve managers is 384 chances to find a streak, so finding several is
the expected outcome and not a discovery. The dossier asks the question directly
instead &mdash; somebody who has drafted in the room can answer it and this
arithmetic cannot.</p>
<p class=note>Nomination premium is the strongest signal here, and it still only
says <em>what</em> somebody nominates. Telling an enforcer from a targeter needs
the self-win rate, which does not survive, so no intent is claimed.</p>
</div>"""


def _method() -> str:
    return """
<h2>How to read a label</h2>
<div class=card>
<p class=sub style="margin-bottom:10px">Every classification is
<b>relative to this league</b>, not to auctions in general. A room where
everybody front-loads has no front-loaders worth naming; the manager worth
flagging is the one who does it more than the people bidding against him. So
each raw number is scored against that season&rsquo;s field and labelled when it
sits more than 0.6&sigma; out.</p>
<p class=sub style="margin-bottom:0">Two consequences. <b>Somebody is always at
the edge</b> &mdash; a label is a statement about rank, so the number behind it is
printed beside it and the league average shows as a tick on the bar. And
<b>four seasons is enough to rank, not to be certain</b>, which is why the
aggregate reports how many seasons agreed rather than averaging labels into one
confident word.</p>
</div>"""


def _season_table(profile: ManagerProfile) -> str:
    rows = ""
    for s in profile.seasons:
        rows += (
            f"<tr><td><b>{s.year}</b></td>"
            f"<td class=n>${s.spend}</td><td class=n>${s.max_price}</td>"
            f"<td class=n>{_pct(s.top3_share)}</td>"
            f"<td class=n>{_pct(s.first_share)}/{_pct(s.middle_share)}/{_pct(s.last_share)}</td>"
            f"<td class=n>{s.premium_index:.2f}</td>"
            f"<td class=n>{s.overpays}/{s.priced_with_reference}</td>"
            f"<td class=n>{s.nominations}</td><td class=n>{_pct(s.self_win_rate)}</td>"
            f"<td>{_e(say(s.spend_shape) or '—')}</td>"
            f"<td>{_e(say(s.pace) or '—')}</td>"
            f"<td>{_e(say(s.nomination_style) or '—')}</td></tr>"
        )
    return f"""<div class=scroll><table>
<tr><th>Season</th><th class=n>Spent</th><th class=n>Top buy</th><th class=n>Top-3 share</th>
<th class=n>1st/2nd/3rd third</th><th class=n>Premium</th><th class=n>Over-pays</th>
<th class=n>Noms</th><th class=n>Self-win</th>
<th>Shape</th><th>Pace</th><th>Nominating</th></tr>{rows}</table></div>"""


def _picks_table(picks: Sequence[DraftPick], team_of: dict[int, str]) -> str:
    rows = ""
    for p in sorted(picks, key=lambda x: x.overall_pick):
        delta = ""
        if p.delta is not None:
            cls = "pos" if p.delta > 0 else "neg"
            delta = f'<span class={cls}>{p.delta:+.0f}</span>'
        rows += (
            f"<tr><td class=n>{p.overall_pick}</td><td>{_e(p.player_name)}</td>"
            f"<td>{_e(p.position)}</td><td>{_e(p.pro_team)}</td>"
            f"<td class=n>${p.price}</td>"
            f"<td class=n>{'' if p.reference is None else f'${p.reference:.0f}'}</td>"
            f"<td class=n>{delta}</td>"
            f"<td>{_e(team_of.get(p.nominating_team_id or 0, '—'))}</td></tr>"
        )
    return f"""<div class=scroll><table>
<tr><th class=n>Pick</th><th>Player</th><th>Pos</th><th>NFL</th><th class=n>Paid</th>
<th class=n>Ref</th><th class=n>&Delta;</th><th>Nominated by</th></tr>{rows}</table></div>"""


def _manager_card(
    profile: ManagerProfile, league: dict[str, float], team_of: dict[int, str]
) -> str:
    pooled: ManagerSeason = profile.pooled
    seasons = ", ".join(str(y) for y in profile.years)

    te = next((s for s in pooled.positions if s.position == "TE"), None)
    chips = (
        _chip(
            say(pooled.spend_shape),
            f"top-3 = {_pct(pooled.top3_share)} of budget · "
            f"{say_agreement(profile.consistency['spend_shape'])}",
            "var(--s1)",
        )
        + _chip(
            say(pooled.pace),
            f"{_pct(pooled.first_share)} spent in the first third · "
            f"{say_agreement(profile.consistency['pace'])}",
            "var(--s2)",
        )
        + _chip(
            say(pooled.nomination_style),
            f"{pooled.nomination_premium:+.1f} vs the going rate · "
            f"{say_agreement(profile.consistency['nomination_style'])}",
            "var(--s3)",
        )
        + (
            _chip(
                "TE money" if te.allocation_index >= 1.25
                else ("TE ignored" if te.allocation_index <= 0.65 else ""),
                f"{te.allocation_index:.2f}× the league's TE split · ${te.spend}",
                "var(--s1)",
            )
            if te
            else ""
        )
    )

    thirds = (
        f'<div class=stack>'
        f'<i style="width:{pooled.first_share*100:.1f}%;background:var(--s1)">'
        f"{_pct(pooled.first_share)}</i>"
        f'<i style="width:{pooled.middle_share*100:.1f}%;background:var(--s2)">'
        f"{_pct(pooled.middle_share)}</i>"
        f'<i style="width:{pooled.last_share*100:.1f}%;background:var(--s3)">'
        f"{_pct(pooled.last_share)}</i></div>"
        '<div class=legend><span><i class="sw" style="background:var(--s1)"></i>'
        f"first third (league {_pct(league['first'])})</span>"
        '<span><i class="sw" style="background:var(--s2)"></i>middle</span>'
        '<span><i class="sw" style="background:var(--s3)"></i>final</span></div>'
    )

    top_positions = sorted(pooled.positions, key=lambda s: -s.spend)
    cap = max((s.allocation_index for s in top_positions), default=1.0)
    position_rows = "".join(
        f'<div class=row><span class=lab>{_e(s.position)}</span>'
        + _bar(
            s.allocation_index, max(cap, 1.6),
            f"{s.allocation_index:.2f}× · ${s.spend}", tick=1.0,
            color="var(--s1)" if s.allocation_index >= 1 else "var(--s2)",
        )
        + "</div>"
        for s in top_positions
    )

    prices = sorted((p.price for p in pooled.picks), reverse=True)[:16]
    top_cap = max(prices) if prices else 1
    spend_rows = "".join(
        f'<div class=row><span class=lab>#{i+1}</span>'
        + _bar(v, top_cap, f"${v}")
        + "</div>"
        for i, v in enumerate(prices)
    )

    pays_up = ", ".join(profile.overpays_at) or "—"
    lets_go = ", ".join(profile.ignores) or "—"

    reaches = "".join(
        f"<tr><td>{_e(p.player_name)}</td><td>{_e(p.position)}</td>"
        f"<td class=n>${p.price}</td><td class=n>${p.reference:.0f}</td>"
        f'<td class="n pos">+{p.delta:.0f}</td></tr>'
        for p in pooled.biggest_reaches
    ) or '<tr><td colspan=5 class=gap>nothing above the reference by a clear margin</td></tr>'

    per_season = "".join(
        f"<details><summary>{s.year} &mdash; every pick "
        f"({len(s.picks)} players, ${s.spend})</summary>"
        f"{_picks_table(s.picks, team_of)}</details>"
        for s in profile.seasons
    )

    return f"""
<div class=card id="m-{_e(profile.owner_id.strip('{}'))}">
  <h3>{_e(profile.manager or 'unknown manager')}</h3>
  <div class=kv><span>seasons <b>{_e(seasons)}</b></span>
    <span>picks <b>{len(pooled.picks)}</b></span>
    <span>spent <b>${pooled.spend:,}</b></span>
    <span>biggest buy <b>${pooled.max_price}</b></span></div>
  <div>{chips}</div>

  <div class="grid two" style="margin-top:16px">
    <div>
      <h4 style="margin:0 0 8px;font-size:13px;color:var(--muted);
        text-transform:uppercase;letter-spacing:.04em">When the money goes out</h4>
      {thirds}
      <h4 style="margin:18px 0 8px;font-size:13px;color:var(--muted);
        text-transform:uppercase;letter-spacing:.04em">Money by position
        <span style="text-transform:none;letter-spacing:0">(1.00× = the league&rsquo;s own split; tick marks it)</span></h4>
      {position_rows}
    </div>
    <div>
      <h4 style="margin:0 0 8px;font-size:13px;color:var(--muted);
        text-transform:uppercase;letter-spacing:.04em">Buys, most to least expensive</h4>
      {spend_rows}
    </div>
  </div>

  <div class=kv style="margin-top:16px">
    <span>pays up at <b>{_e(pays_up)}</b></span>
    <span>lets go cheap <b>{_e(lets_go)}</b></span>
    <span>ESPN accounts <b>{len(profile.accounts) or 1}</b></span>
  </div>

  <h4 style="margin:18px 0 6px;font-size:13px;color:var(--muted);
    text-transform:uppercase;letter-spacing:.04em">Biggest reaches</h4>
  <div class=scroll><table><tr><th>Player</th><th>Pos</th><th class=n>Paid</th>
  <th class=n>Reference</th><th class=n>Over</th></tr>{reaches}</table></div>

  <h4 style="margin:18px 0 6px;font-size:13px;color:var(--muted);
    text-transform:uppercase;letter-spacing:.04em">Season by season</h4>
  {_season_table(profile)}
  {per_season}
</div>"""


def render_report(
    history: History,
    profiles: Sequence[ManagerProfile],
    *,
    generated: str = "",
    scores=None,
) -> str:
    league_first = sum(
        p.first_share for pr in profiles for p in [pr.pooled] if p
    ) / max(len(profiles), 1)
    league = {"first": league_first}

    from ffa.config.identity import fold_owner_id

    by_owner = {fold_owner_id(p.owner_id): p for p in profiles}
    team_of: dict[int, str] = {}
    for season in history.seasons:
        for team_id, owner in season.owners.items():
            match = by_owner.get(fold_owner_id(owner))
            if match and match.manager:
                team_of[team_id] = match.manager

    seasons_used = ", ".join(str(y) for y in history.years)
    index = " · ".join(
        f'<a href="#m-{_e(p.owner_id.strip("{}"))}">{_e(p.manager)}</a>' for p in profiles
    )
    cards = "".join(_manager_card(p, league, team_of) for p in profiles)

    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Draft history — {_e(history.league_name)}</title>
<style>{CSS}</style></head><body><div class=wrap>
<h1>{_e(history.league_name or 'League')} — draft history</h1>
<p class=sub>Every auction ESPN still serves for league {history.league_id},
read pick by pick: what each manager paid, when they spent it, what they
nominated, and who they nominated it at. Seasons used: <b>{_e(seasons_used)}</b>.
{_e(generated)}</p>
{_provenance(history)}
{_evidence(scores)}
{_method()}
<h2>Managers</h2>
<p class=sub>{index}</p>
{cards}
</div></body></html>"""
