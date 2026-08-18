"""Reading prior seasons, and the arithmetic that turns them into a read.

The analysis is the part that can be wrong, so all of it runs on synthetic
payloads with no network and no cache. What the tests mostly pin is the
difference between *absent* and *zero*: a season ESPN never served, a draft that
was not an auction, a player with no reference price, and — the one that
actually bit — a season where `auctionValueAverage` comes back as a column of
zeroes rather than as nothing at all.
"""

from __future__ import annotations

import pytest

from ffa.history.fetch import price_reference, season_from_payload
from ffa.history.metrics import _gini, _spearman, analyse_season, build_profiles
from ffa.history.schema import DraftPick, History, SeasonDraft, SeasonGap
from ffa.history.suggest import suggest_dossiers

OWNERS = {1: "{A}", 2: "{B}", 3: "{C}"}


def player(pid, name, pos="RB", team="KC", value=None):
    entry = {
        "id": pid,
        "player": {
            "id": pid,
            "fullName": name,
            "defaultPositionId": {"QB": 1, "RB": 2, "WR": 3, "TE": 4}.get(pos, 2),
            "proTeamId": {"KC": 12, "PHI": 21, "BUF": 2}.get(team, 12),
            "ownership": {"auctionValueAverage": value or 0},
        },
    }
    return entry


def payload(*, draft_type="AUCTION", picks=None, players=None, year=2025):
    return {
        "year": year,
        "league_id": 9,
        "mSettings": {
            "settings": {
                "size": 3,
                "name": "Test",
                "draftSettings": {"type": draft_type, "auctionBudget": 200},
            }
        },
        "mSettings_status": "ok",
        "mTeam": {
            "teams": [
                {"id": t, "primaryOwner": o, "name": f"Team {t}"}
                for t, o in OWNERS.items()
            ]
        },
        "mDraftDetail": {"draftDetail": {"picks": picks if picks is not None else []}},
        "kona_player_info": {"players": players or []},
    }


def pick(n, team, pid, price, nominator=None):
    return {
        "overallPickNumber": n,
        "teamId": team,
        "playerId": pid,
        "bidAmount": price,
        "nominatingTeamId": nominator if nominator is not None else team,
    }


# --- what came back, and what did not ----------------------------------------------


def test_a_season_espn_never_served_is_a_stated_gap():
    """"No data for 2019" and "2019 was a snake draft" are different claims."""
    result = season_from_payload({"year": 2019, "mSettings": None, "mSettings_status": "HTTP 202"})

    assert isinstance(result, SeasonGap)
    assert "202" in result.reason


def test_a_non_auction_season_is_excluded_with_the_reason():
    result = season_from_payload(payload(draft_type="OFFLINE", year=2021))

    assert isinstance(result, SeasonGap)
    assert "OFFLINE" in result.reason and "AUCTION" in result.reason


def test_a_season_with_picks_but_no_prices_is_excluded():
    """An unpriced board would read as everybody buying everything for nothing."""
    picks = [pick(i, 1, 100 + i, 0) for i in range(1, 6)]
    result = season_from_payload(payload(picks=picks))

    assert isinstance(result, SeasonGap)
    assert "bidAmount" in result.reason


def test_an_unfilled_skeleton_is_not_a_draft():
    picks = [{"overallPickNumber": i, "teamId": 1, "playerId": -1} for i in range(1, 10)]
    result = season_from_payload(payload(picks=picks))

    assert isinstance(result, SeasonGap)
    assert "skeleton" in result.reason


# --- the price reference is not the same field every year ---------------------------


def test_a_column_of_zero_auction_values_is_not_a_reference():
    """2025 returns `auctionValueAverage: 0.0` for every player.

    Trusting it would price the whole league at zero and report that everybody
    massively overpaid for everybody — the failure this fallback exists for.
    """
    players = [player(1, "A", value=0), player(2, "B", value=0)]
    for entry in players:
        entry["player"]["draftRanksByRankType"] = {"PPR": {"auctionValue": 30}}

    reference, source = price_reference({"players": players})

    assert reference == {1: 30.0, 2: 30.0}
    assert "editorial" in source


def test_the_two_reference_fields_are_merged_rather_than_chosen_between():
    """They disagree on coverage in both directions, and a player with no
    reference is a purchase that cannot be judged at all."""
    a = player(1, "A", value=40)
    b = player(2, "B")
    b["player"]["draftRanksByRankType"] = {"PPR": {"auctionValue": 12}}

    reference, source = price_reference({"players": [a, b]})

    assert reference == {1: 40.0, 2: 12.0}
    assert "consensus" in source and "editorial" in source


def test_consensus_wins_where_both_exist():
    """An average of what drafters paid beats one desk's opinion."""
    a = player(1, "A", value=40)
    a["player"]["draftRanksByRankType"] = {"PPR": {"auctionValue": 12}}

    reference, _ = price_reference({"players": [a]})

    assert reference == {1: 40.0}


def test_no_reference_at_all_says_so_rather_than_inventing_one():
    reference, source = price_reference({"players": [player(1, "A")]})
    assert reference == {} and source == "none available"


def test_references_are_scaled_to_the_money_actually_spent():
    """Otherwise a season covered by 160 editorial values and one covered by 350
    consensus averages cannot sit in the same table."""
    players = [player(i, f"P{i}", value=10) for i in range(1, 4)]
    picks = [pick(i, i, i, 20) for i in range(1, 4)]  # league paid 2x the sheet

    season = season_from_payload(payload(picks=picks, players=players))

    assert [p.reference for p in season.picks] == [20.0, 20.0, 20.0]
    assert all(p.overpay_ratio == 1.0 for p in season.picks)


def test_a_player_with_no_reference_is_excluded_not_zeroed():
    """A zero consensus would make every purchase look like a chase."""
    players = [player(1, "Known", value=10)]
    picks = [pick(1, 1, 1, 10), pick(2, 2, 999, 30)]

    season = season_from_payload(payload(picks=picks, players=players))

    unknown = next(p for p in season.picks if p.player_id == 999)
    assert unknown.reference is None
    assert unknown.delta is None and unknown.overpay_ratio is None


def test_a_pick_carries_everything_the_report_needs():
    players = [player(7, "Saquon Barkley", pos="RB", team="PHI", value=60)]
    picks = [pick(1, 2, 7, 70, nominator=3)]

    season = season_from_payload(payload(picks=picks, players=players))
    p = season.picks[0]

    assert (p.player_name, p.position, p.pro_team) == ("Saquon Barkley", "RB", "PHI")
    assert (p.overall_pick, p.price, p.team_id, p.nominating_team_id) == (1, 70, 2, 3)
    assert p.self_nominated is False


# --- the statistics -----------------------------------------------------------------


def test_gini_is_zero_when_every_pick_cost_the_same():
    assert _gini([10, 10, 10, 10]) == pytest.approx(0.0)


def test_gini_approaches_one_when_one_pick_took_everything():
    assert _gini([100, 1, 1, 1]) > 0.6


def test_spearman_is_positive_when_the_sequence_tracks_the_ranking():
    assert _spearman([0, 1, 2, 3], [0, 1, 2, 3]) == pytest.approx(1.0)
    assert _spearman([0, 1, 2, 3], [3, 2, 1, 0]) == pytest.approx(-1.0)


def test_spearman_refuses_a_sample_too_small_to_mean_anything():
    assert _spearman([0, 1], [1, 0]) == 0.0


# --- classification is relative to the room -----------------------------------------


def _league(prices_by_team, nominators=None):
    """Three managers, fifteen picks each, prices you specify."""
    picks = []
    n = 1
    for team, prices in sorted(prices_by_team.items()):
        for i, price in enumerate(prices):
            nominator = (nominators or {}).get(team, team)
            picks.append(
                DraftPick(
                    overall_pick=n, team_id=team, player_id=n, price=price,
                    position="RB", pro_team="KC", nominating_team_id=nominator,
                    reference=float(price),
                )
            )
            n += 1
    return SeasonDraft(
        year=2025, league_id=9, team_count=3, budget=200,
        picks=tuple(picks), owners=OWNERS,
    )


def test_the_manager_who_concentrates_most_is_the_stars_and_scrubs_one():
    draft = _league({
        1: [90, 80, 20] + [1] * 5,
        2: [40, 40, 40, 40, 20] + [1] * 3,
        3: [30, 30, 30, 30, 30, 30] + [1] * 2,
    })

    labels = {s.team_id: s.spend_shape for s in analyse_season(draft)}

    assert labels[1] == "stars_and_scrubs"
    assert labels[3] == "value_hunter"


def test_a_flat_field_produces_no_labels_because_nobody_stands_out():
    """A room where everybody drafts identically has no archetypes in it.

    An absolute threshold would label all three the same and call it a finding.
    """
    draft = _league({t: [50, 40, 30, 20] + [1] * 4 for t in (1, 2, 3)})

    shapes = {s.spend_shape for s in analyse_season(draft)}

    assert shapes == {"balanced"}


def test_spending_early_reads_as_front_loading():
    # Team 1's picks come first in overall order, so its money goes out first.
    draft = _league({1: [60] * 3 + [1] * 5, 2: [30] * 6 + [1] * 2, 3: [20] * 9})

    paces = {s.team_id: s.pace for s in analyse_season(draft)}

    assert paces[1] == "front_loads"


def test_chasing_is_not_judged_off_a_handful_of_priced_picks():
    """Five referenced picks is not evidence, and a label implies it is."""
    picks = [
        DraftPick(overall_pick=i, team_id=1, player_id=i, price=10,
                  reference=5.0 if i < 4 else None, position="RB")
        for i in range(1, 9)
    ]
    draft = SeasonDraft(year=2025, league_id=9, team_count=3, budget=200,
                        picks=tuple(picks), owners=OWNERS)

    season = next(s for s in analyse_season(draft) if s.team_id == 1)

    assert season.chases == ""
    assert any("too few priced picks" in n for n in season.notes)


def test_a_season_without_nomination_data_says_so_instead_of_guessing():
    picks = tuple(
        DraftPick(overall_pick=i, team_id=1, player_id=i, price=10, position="RB")
        for i in range(1, 16)
    )
    draft = SeasonDraft(year=2025, league_id=9, team_count=3, budget=200,
                        picks=picks, owners=OWNERS)

    season = next(s for s in analyse_season(draft) if s.team_id == 1)

    assert season.nomination_style == ""
    assert any("no nomination order" in n for n in season.notes)


def test_only_a_manager_who_nominated_enough_gets_a_reading():
    """Nomination labels describe *what* somebody puts up, never why.

    Telling an enforcer from a targeter needs the self-win rate, which does not
    survive its null, so the classifier reports the premium and stops.
    """
    draft = _league(
        {1: [20] * 8, 2: [20] * 8, 3: [20] * 8},
        nominators={1: 1, 2: 1, 3: 1},  # team 1 nominates everything
    )
    styles = {s.team_id: s.nomination_style for s in analyse_season(draft)}

    assert styles[1] in {"nominates_high", "nominates_low", "no_pattern"}
    assert styles[2] == "" and styles[3] == ""


def test_no_intent_label_is_ever_produced():
    """`enforcer` and `targeter` are claims about motive the data cannot make."""
    draft = _league({1: [90, 80, 20] + [1] * 5, 2: [40] * 5 + [1] * 3,
                     3: [30] * 6 + [1] * 2})

    produced = {s.nomination_style for s in analyse_season(draft)}

    assert not produced & {"enforcer", "targets", "best_available"}


# --- pooling across seasons ----------------------------------------------------------


def test_a_profile_reports_how_many_seasons_agreed_rather_than_a_bare_label():
    draft_a = _league({1: [90, 80, 20] + [1] * 5, 2: [40] * 5 + [1] * 3,
                       3: [30] * 6 + [1] * 2})
    draft_b = SeasonDraft(**{**draft_a.__dict__, "year": 2024})
    profiles = build_profiles(History(seasons=(draft_b, draft_a)), {"{A}": "Ann"})

    ann = next(p for p in profiles if p.owner_id == "{A}")

    assert ann.years == (2024, 2025)
    assert ann.consistency["spend_shape"] == "2/2 stars_and_scrubs"


def test_free_agents_are_not_an_nfl_team_anybody_is_a_homer_about():
    picks = tuple(
        DraftPick(overall_pick=i, team_id=1, player_id=i, price=5,
                  position="RB", pro_team="FA")
        for i in range(1, 12)
    )
    draft = SeasonDraft(year=2025, league_id=9, team_count=3, budget=200,
                        picks=picks, owners=OWNERS)

    profile = next(p for p in build_profiles(History(seasons=(draft,))) if p.owner_id == "{A}")

    assert "FA" not in profile.homer_teams
    assert all(a.pro_team != "FA" for a in profile.pooled.affinities)


# --- the dossier draft ---------------------------------------------------------------


def _profiles_for_suggest():
    draft = _league({1: [90, 80, 20] + [1] * 5, 2: [40] * 5 + [1] * 3,
                     3: [30] * 6 + [1] * 2})
    other = SeasonDraft(**{**draft.__dict__, "year": 2024})
    return build_profiles(History(seasons=(other, draft)), {"{A}": "Ann"})


def test_a_suggestion_is_keyed_by_team_id_the_way_the_importer_expects():
    suggested = suggest_dossiers(_profiles_for_suggest(), OWNERS)

    assert "1" in suggested
    assert suggested["1"]["real_name"] == "Ann"
    assert suggested["1"]["spend_shape"] == "stars_and_scrubs"


def test_every_suggestion_says_where_it_came_from():
    """Whoever reads this in three seasons should not have to guess whether a
    field was somebody's memory or a script's arithmetic."""
    suggested = suggest_dossiers(_profiles_for_suggest(), OWNERS)

    assert "Derived from ESPN draft history" in suggested["1"]["notes"]
    assert "review before trusting" in suggested["1"]["notes"]
    assert "permutation test" in suggested["1"]["notes"]


def test_only_signals_that_cleared_a_null_are_suggested():
    """A suggestion is one `ffa dossier import` away from being an observation.

    Homer teams (z = −0.3), chasing (−1.1) and nomination style all failed their
    null tests, so none of them may reach the file — a false observation is far
    worse than a missing one.
    """
    suggested = suggest_dossiers(_profiles_for_suggest(), OWNERS)

    banned = {"homer_teams", "avoids_teams", "chases", "nomination_style"}
    assert not {k for entry in suggested.values() for k in entry} & banned


def test_positional_bias_is_filtered_to_the_position_that_survived():
    """TE clears its null at +3.0; RB, WR and QB sit between +0.1 and +1.1.

    Dropping both lists would throw away a survivor; keeping them whole would
    ship three failures.
    """
    from ffa.history.suggest import SURVIVING_POSITIONS

    suggested = suggest_dossiers(_profiles_for_suggest(), OWNERS)

    for entry in suggested.values():
        for field in ("overpays_at", "ignores"):
            assert set(entry.get(field, [])) <= SURVIVING_POSITIONS


def test_a_manager_with_one_season_is_left_out_of_the_suggestions():
    draft = _league({1: [90, 80, 20] + [1] * 5, 2: [40] * 5 + [1] * 3,
                     3: [30] * 6 + [1] * 2})
    profiles = build_profiles(History(seasons=(draft,)), {"{A}": "Ann"})

    assert suggest_dossiers(profiles, OWNERS) == {}


def test_a_manager_who_left_the_league_is_not_suggested_into_a_seat():
    """Brian Cona held team 6 through 2023. His read must not land on whoever
    holds that seat now — the SWID is the anchor, not the chair."""
    suggested = suggest_dossiers(_profiles_for_suggest(), {1: "{SOMEBODY-ELSE}"})

    assert suggested == {}


def test_the_suggestion_round_trips_through_the_dossier_importer():
    """The two formats have to agree or the whole hand-off is decorative."""
    from ffa.dossier.ingest import apply_payload
    from ffa.dossier.store import DossierBook

    suggested = suggest_dossiers(_profiles_for_suggest(), OWNERS)
    book, report = apply_payload(
        DossierBook({}, owners=OWNERS), suggested,
        seats=OWNERS, labels={}, today="2026-08-18",
    )

    assert report.rejected == []
    assert book.for_team(1).spend_shape.value == "stars_and_scrubs"
    assert book.for_team(1).real_name == "Ann"
