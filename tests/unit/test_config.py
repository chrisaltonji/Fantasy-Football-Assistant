from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ffa.config.loader import (
    config_to_dict,
    load_config,
    load_assist_credentials,
    load_credentials,
    load_dotenv,
    parse_config,
    write_config,
)
from ffa.config.schema import (
    AnthropicCredentials,
    ConfigError,
    EspnCredentials,
)
from ffa.domain.enums import Position, RosterSlot

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "league.example.toml"


def example_data() -> dict:
    return tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))


# --- the shipped template must always parse; it is the user's starting point ---


def test_example_config_parses():
    config = parse_config(example_data())
    assert config.is_auction
    assert config.budget == 200
    assert config.team_count == 12
    assert config.roster[RosterSlot.RB] == 2
    assert Position.RB in config.flex_positions


def test_draftable_slots_excludes_ir():
    config = parse_config(example_data())
    # 1+2+2+1+1+1+1+7 = 16 draftable; IR is not drafted into.
    assert config.draftable_slots == 16
    assert RosterSlot.IR in config.roster
    assert config.starting_slots == 9


def test_round_trip_through_toml(tmp_path: Path):
    original = parse_config(example_data())
    target = tmp_path / "league.toml"
    write_config(original, target)
    assert parse_config(tomllib.loads(target.read_text())) == original


def test_slots_for_position_prefers_native_then_flex_then_bench():
    config = parse_config(example_data())
    assert config.slots_for_position(Position.RB) == (
        RosterSlot.RB,
        RosterSlot.FLEX,
        RosterSlot.BE,
    )
    # QB isn't flex-eligible in the default config.
    assert config.slots_for_position(Position.QB) == (RosterSlot.QB, RosterSlot.BE)


# --- validation must fail loudly rather than produce wrong advice later ---


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda d: d["draft"].update(type="SNAKE"), "built for AUCTION"),
        (lambda d: d["draft"].update(budget=0), "must be positive"),
        (lambda d: d["teams"].update(count=1), "at least 2"),
        (lambda d: d["draft"].update(budget=5), "less than the 16 draftable"),
        (lambda d: d["polling"].update(interval_seconds=0.1), "rate-limiting"),
        (lambda d: d["league"].update(year=1999), "year looks wrong"),
    ],
)
def test_invalid_configs_raise(mutate, fragment):
    data = example_data()
    mutate(data)
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert fragment in str(exc.value)


def test_missing_required_key_names_the_key():
    data = example_data()
    del data["league"]["league_id"]
    with pytest.raises(ConfigError, match=r"\[league\].league_id"):
        parse_config(data)


def test_unknown_roster_slot_lists_valid_slots():
    data = example_data()
    data["roster"]["SUPERFLEX"] = 1
    with pytest.raises(ConfigError, match="unknown slot"):
        parse_config(data)


def test_missing_config_file_explains_how_to_create_one(tmp_path: Path):
    with pytest.raises(ConfigError, match="ffa config init"):
        load_config(tmp_path / "nope.toml")


def test_config_to_dict_omits_zero_count_slots():
    data = example_data()
    data["roster"]["K"] = 0
    config = parse_config(data)
    assert RosterSlot.K not in config.roster
    assert "K" not in config_to_dict(config)["roster"]


# --- credentials ---


def test_dotenv_parsing_handles_comments_and_quotes(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text('# comment\nESPN_S2="abc123"\nESPN_SWID={ABC}\n\n')
    assert load_dotenv(env) == {"ESPN_S2": "abc123", "ESPN_SWID": "{ABC}"}


def test_dotenv_rejects_malformed_line(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ESPN_S2 abc\n")
    with pytest.raises(ConfigError, match="expected KEY=value"):
        load_dotenv(env)


def test_missing_credentials_are_optional_unless_required(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ESPN_S2", raising=False)
    monkeypatch.delenv("ESPN_SWID", raising=False)
    empty = tmp_path / ".env"
    empty.write_text("")

    assert load_credentials(empty) is None
    with pytest.raises(ConfigError, match="ESPN_S2 and ESPN_SWID"):
        load_credentials(empty, required=True)


def test_swid_braces_are_restored():
    creds = EspnCredentials(espn_s2="s2value", swid="ABC-123")
    assert creds.as_cookies() == {"espn_s2": "s2value", "SWID": "{ABC-123}"}
    already = EspnCredentials(espn_s2="s2value", swid="{ABC-123}")
    assert already.as_cookies()["SWID"] == "{ABC-123}"


def test_credentials_never_appear_in_repr():
    creds = EspnCredentials(espn_s2="supersecret", swid="{SWIDSECRET}")
    assert "supersecret" not in repr(creds)
    assert "SWIDSECRET" not in repr(creds)


# --- the assist key -----------------------------------------------------------


def test_the_assist_key_comes_from_env_or_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-from-file" + chr(10), encoding="utf-8")
    assert load_assist_credentials(env).api_key == "sk-ant-from-file"

    # A real environment variable wins, so CI and one-off overrides work
    # without editing a file.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert load_assist_credentials(env).api_key == "sk-ant-from-env"


def test_no_assist_key_is_not_an_error_by_itself(tmp_path, monkeypatch):
    """Absent is the normal case — the draft runs identically without one."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert load_assist_credentials(tmp_path / "missing.env") is None


def test_asking_for_assist_without_a_key_explains_both_ways_out(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError) as caught:
        load_assist_credentials(tmp_path / "missing.env", required=True)

    message = str(caught.value)
    assert ".env" in message          # how to fix it
    assert "--assist" in message      # and how to proceed without it


def test_the_assist_key_never_appears_in_a_repr():
    """This repo is public and a key in a traceback is a key in a screenshot."""
    creds = AnthropicCredentials(api_key="sk-ant-supersecret")
    assert "supersecret" not in repr(creds)
