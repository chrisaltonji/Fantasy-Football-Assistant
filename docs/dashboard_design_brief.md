# Draft dashboard — design brief

**You are designing one screen.** The plumbing exists and is not yours to
change: a local server already computes the whole payload and hands it to your
page. What you own is everything the user sees.

Hand back **one self-contained HTML file**. See [Integration](#integration).

---

## 1. The situation you are designing for

A live $200 fantasy auction draft. 180 players sell over about three hours, one
at a time, roughly 30–60 seconds each. The user is at a laptop with the ESPN
draft room open in another window, **being bid against while reading your
screen**.

Between the moment a player is nominated and the moment the gavel falls, they
need one question answered:

> **Should I bid on this guy, and how high can I go before someone else stops
> being able to follow me?**

Everything on screen either serves that question or gets out of the way. A
number that takes four seconds to find is a number they will not use.

### The one insight the design exists to make obvious

**Money alone does not make a rival dangerous.** A team with $90 left that has
already filled every RB and FLEX slot *cannot meaningfully bid on a running
back*. They look terrifying and are harmless.

The payload gives you both halves — every rival's budget and every rival's
positional need. Joining them so that "who can actually take this player from
me" reads **at a glance, without cross-referencing two places on screen**, is
the single thing this dashboard is for. Every other panel is support.

There is a flag for it: `is_live` on each threat means *can afford him **and**
has a starting slot for him*. That is the signal.

---

## 2. What the user is doing while looking at this

- **Glancing, not reading.** Assume two seconds of attention at a time.
- **Not clicking.** Input happens in a terminal beside this. The dashboard is
  **display-only** — nothing on it changes the draft. Click-to-expand for detail
  is welcome; there is nothing to submit.
- **Possibly at half-width**, tiled next to the ESPN window.
- **In a dim room at 8pm.**

---

## 3. What must be on screen

Grouped by what it answers, not by how you should lay it out. Panel shapes,
grouping, order within a tier, and everything visual are yours.

| # | Answers | Data |
|---|---|---|
| 1 | **What can I spend right now?** Remaining budget, hard ceiling, the advisable bid when someone is up, roster slots filled, which starting slots I still need. | `me` |
| 2 | **Who is up, and who can take him?** Player, position, sheet value, that value at current market, advisable bid, hard ceiling, and rivals ranked by true ceiling with `is_live`. | `nomination` |
| 3 | **Where does everyone stand?** One entry per rival joining budget with positional need. | `teams[]` |
| 4 | **What is left?** Remaining players per position, split elite / startable / bench. | `scarcity` |
| 5 | **Is the room hot or cold?** Inflation ratio, money spent, money still out there. | `market` |
| 6 | **Am I on my own plan?** Planned vs actual spend per position, and whether the plan is still affordable. | `strategy` |
| 7 | **When do I nominate?** Whose turn it is, how many until mine. | `nomination_plan` |
| 8 | **What just happened?** Last ~10 sales with price and delta vs sheet. | `recent_sales` |
| 9 | **What should I know?** Non-modal notices. | `warnings` |

### Priority, when space runs out

1. My ceiling and my remaining budget
2. The nominated player and who can actually outbid me
3. Rival budgets joined with positional need
4. Scarcity and market temperature
5. Everything else

**Degrade in reverse order.** Tiers 1–2 must survive any viewport.

---

## 4. Rules that are correctness, not taste

Short list, and the only place this brief is prescriptive. Each one exists
because breaking it produces a screen that is confidently wrong.

1. **Never render a floor as a plain number.** A sale can be recorded before its
   price is known; those are charged at $1, so that team's `remaining` is a
   *floor*, not a fact. When `remaining_is_floor` is true, show `$142+` or
   equivalent. The number deliberately overstates a rival's ammunition — that is
   the safe direction — but only if the user can see that is what they are
   looking at.

2. **Judgment and arithmetic must not look alike.** `max_advisable_bid` is an
   opinion about what he is worth. `max_legal_bid` is a number you physically
   cannot exceed. Same for `plan_cap`, which is a preference the user declared
   and which the engine deliberately does *not* enforce. Three different kinds
   of number; they should not read as three instances of one kind.

3. **`is_live` is the signal, not budget size.** See §1.

4. **Key positional need off `starter_gaps`, not `open_slots_by_pos`.** The
   second includes bench and is almost always non-zero, so it makes everyone
   look like a threat forever. The first is starting slots only.

5. **Never key off `name`, and never use a team id as an array index.** Managers
   rename teams mid-draft. `label` is what to display, `owner_id` is identity,
   `name` is reference only. Team ids have gaps — this league runs 1–5 and 7–13,
   with no team 6.

6. **Absent data is a state to render, not a zero.** `inflation_ratio` is `null`
   until five priced sales have landed (early noise would swing everything that
   reads it). `scarcity` is `{}` with no reference file. `nomination` is `null`
   most of the time. `strategy` is `null` when no plan is declared. Each needs a
   deliberate "not yet" rendering — never a gauge pinned at zero.

7. **Never announce.** No toasts, no flashing, no modal steal, no sound. The
   board re-renders silently and lets the user notice in their own time. They
   are being bid against; stealing focus is the one unforgivable thing.

8. **Degrade, never blank.** If a poll fails, keep the last good board on screen
   and mark it stale. A number the user can see and distrust beats an empty
   panel mid-draft.

---

## 5. What is entirely yours

Layout, grid, panel shapes and grouping. Palette and theme (dark is likely right
for the room, but argue for whatever you want). Typography and scale. Density.
Charts, sparklines, meters — or none. Hover and click-to-expand behaviour.
Motion, if any. How the rival join is expressed visually — dimming, ordering,
grouping, stripes, something better. Iconography. Empty-state copy. Which of the
nine groups share a panel and which get their own.

The existing implementation is one answer, not the answer. Feel free to ignore
it entirely.

---

## 6. The data

**Two real payloads to design against, not mockups:**

- `docs/sample_state.json` — mid-draft, **a player on the block**, 11 rivals
  live, market inflated. The highest-attention state.
- `docs/sample_state_idle.json` — mid-draft, **nothing on the block**. What the
  screen shows most of the time. Do not treat it as the edge case.

Top-level keys:

```
schema_version  draft_id  generated_at  initialized  reference
league  me  teams[]  nomination  nomination_plan  strategy
market  recent_sales[]  scarcity  warnings[]
```

The fields worth knowing before you open the files:

**`me` and each entry in `teams[]`** — same shape.
`label` (display name), `owner_id` (identity), `is_me`, `spent`, `remaining`,
`remaining_is_floor`, `unknown_price_count`, `max_legal_bid`, `is_overspent`,
`roster_count`, `open_slots`, `starter_gaps` (use this), `open_slots_by_pos`
(don't), `roster[]` with per-player `price`, `price_known`, `price_provenance`.

**`nomination`** — `null` unless a player is up. When present: `name`,
`position`, `reference_value`, and `guidance` carrying `max_advisable_bid`,
`suggested_low`/`suggested_high`, `max_legal_bid`, `safe_legal_bid`,
`ceiling_is_optimistic`, `inflated_value`, `fills_starter_gap`,
`contested_ceiling`, `plan_cap`, `exceeds_plan_cap`, `reasons[]` (plain-English
strings, already written), `threats[]`, and `pace_reads[]`.

Each **threat**: `team_id`, `label`, `remaining`, `remaining_is_floor`,
`max_legal_bid`, `open_at_position`, `is_live`. Pre-sorted, live first.

Each **pace read** compares a rival to their own previous drafts —
`actual_share`, `expected_share`, `dollars_vs_script`, `seasons`, `is_thin`,
`is_notable`. Only show the notable ones; the array is already trimmed to where
the record discriminates. This is the one block that is about *previous* drafts
rather than tonight, and it should not look like tonight's arithmetic.

**`scarcity`** — keyed by position: `elite`, `startable`, `bench`,
`total_remaining`, `starting_demand`, `top_value_remaining`, `is_drying_up`.
"Startable" means against *this league's* real starting demand, not a generic
tier column.

**`market`** — `inflation_ratio` (nullable), `read`
(`inflated`/`neutral`/`deflated`/`unknown`), `dollars_spent`,
`dollars_remaining`, `reference_spent`, `sales`, `unknown_prices`.

**`strategy`** — `null` if no plan. Else `archetype`, `planned_total`,
`spent_total`, `remaining`, `still_calls_for`, `shortfall` (>0 means the plan is
no longer affordable — the sharpest number in the block), `slack`,
`max_on_one_player`, `biggest_buy`, `breached_cap`, `positions[]` with
`planned`/`spent`/`variance`/`is_material`.

**`nomination_plan`** — `order_known` (false means say nothing about turns),
`turns_taken`, `total_nominations`, `on_the_clock`, `upcoming[]`, `my_next`,
`nominations_until_mine`, `my_turns_left`, `disagreement` (a string; when
non-empty the board and the schedule disagree and the board wins).

**`recent_sales[]`** — `name`, `position`, `price`, `price_known`,
`price_provenance`, `reference_value`, `delta` (already computed), `team_id`.

---

## Integration

Hand back **one self-contained HTML file**: inline CSS and JS, no build step, no
external requests. It will be served from a local origin at `/`.

Three endpoints exist. You need the first two:

| Endpoint | Returns |
|---|---|
| `GET /state.json` | the full payload above, recomputed from the live draft |
| `GET /meta.json` | `{ "poll_interval": 2.0, "run_dir": "..." }` |
| `GET /` | your page |

Poll `state.json` on an interval — read it from `meta.json` rather than
hardcoding. The payload is a few KB and cheap to re-fetch; there is no
websocket and no diffing protocol to honour. Re-render from whole state.

To see it running against a real draft:

```bash
ffa dashboard --open
```

Nothing else about the plumbing is your problem. If you need a field the payload
does not carry, say so rather than deriving it on the page — the engine computes
every number exactly once, and a second calculation in the browser is how two
surfaces start disagreeing about the same draft.
