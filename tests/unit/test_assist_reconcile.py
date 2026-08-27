"""Read against outcome — the join the Narrator and the Grader both need."""

from __future__ import annotations

from ffa.assist.budget import PRICES, SpendGuard, cost_cents
from ffa.assist.reconcile import compare

READ = {"rivals": [
    {"team_id": 9, "lo": 31, "hi": 39},
    {"team_id": 12, "lo": 20, "hi": 28},
]}


def test_a_sale_above_the_band_reads_as_over():
    out = compare(READ, price=47, winner_team_id=9)
    assert (out["expected_lo"], out["expected_hi"]) == (20, 39)
    assert out["band"] == "over" and out["delta"] == 8


def test_a_sale_inside_the_band_has_no_delta():
    out = compare(READ, price=33, winner_team_id=9)
    assert out["band"] == "inside" and out["delta"] == 0


def test_a_sale_below_the_band_reads_as_under():
    out = compare(READ, price=12, winner_team_id=12)
    assert out["band"] == "under" and out["delta"] == -8


def test_it_says_whether_the_winner_was_one_of_the_named():
    named = compare(READ, price=40, winner_team_id=9)
    assert named["winner_was_named"] is True and named["winner_expected_hi"] == 39

    surprise = compare(READ, price=40, winner_team_id=3)
    assert surprise["winner_was_named"] is False


def test_no_read_and_no_price_are_both_ordinary():
    assert compare(None, price=40, winner_team_id=9)["had_read"] is False
    assert compare(READ, price=None, winner_team_id=9)["band"] == "unknown"
    assert compare({"rivals": []}, price=40, winner_team_id=9)["band"] == "unknown"


# --- cost -------------------------------------------------------------------


def test_a_cached_call_costs_a_fraction_of_an_uncached_one():
    """The whole reason the prefix is built carefully."""
    cached = cost_cents({"cache_read": 5000, "input": 1800, "output": 700}, "claude-opus-5")
    uncached = cost_cents({"input": 6800, "output": 700}, "claude-opus-5")
    assert cached < uncached


def test_the_room_call_lands_near_three_cents():
    cents = cost_cents(
        {"cache_read": 5000, "input": 1800, "output": 700}, "claude-opus-5"
    )
    assert 2 <= cents <= 4, f"{cents}c per nomination changes the whole budget"


def test_an_unknown_model_is_priced_as_the_default_rather_than_free():
    assert cost_cents({"input": 1_000_000}, "something-new") > 0


def test_the_cap_is_per_draft_and_mutes_rather_than_throttles():
    guard = SpendGuard(cap_dollars=1.0)
    assert not guard.exceeded
    guard.record(990_000)                 # 99c, in micros
    assert not guard.exceeded
    guard.record(20_000)                  # 2c more, over the dollar
    assert guard.exceeded and "muted" in guard.reason()


def test_the_cap_counts_in_micros_so_a_third_of_a_cent_is_a_third_of_a_cent():
    """The tick costs ~0.29c and is the most numerous call by far. Counted in
    whole cents it billed a full penny each — 3.4x over — and the cap is a safety
    rail, so a rail that reads high fires early and wastes budget that was
    deliberately granted."""
    guard = SpendGuard(cap_dollars=1.0)
    for _ in range(100):
        guard.record(2_900)               # 0.29c apiece

    assert guard.spent_dollars == 0.29
    assert not guard.exceeded


def test_a_one_hour_ttl_write_is_priced_at_double_not_a_quarter_over():
    """`prompts.CACHE_TTL` asks for an hour. The premium is 1.25x at the default
    five minutes and 2x at an hour, and this priced the wrong one — under-
    reporting every write by 37%, which is the unsafe direction."""
    from ffa.assist.budget import CACHE_WRITE, cost_dollars

    assert CACHE_WRITE == 2.0
    # 1M cache-write tokens on Opus 5 at $5/MTok input = $10 at 2x.
    assert cost_dollars({"cache_creation": 1_000_000}, "claude-opus-5") == 10.0


def test_every_priced_model_has_both_rates():
    assert all(len(v) == 2 for v in PRICES.values())
