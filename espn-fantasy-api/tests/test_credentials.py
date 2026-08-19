"""Credential loading, and the folding that keeps a copy-pasted SWID working."""

from __future__ import annotations

import pytest

from espn_fantasy.credentials import (
    EspnCredentials,
    fold_owner_id,
    load_credentials,
    load_dotenv,
)
from espn_fantasy.errors import EspnApiError


def write_env(tmp_path, text: str):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_dotenv_reads_pairs_and_ignores_comments_and_blanks(tmp_path):
    path = write_env(tmp_path, "# a comment\n\nESPN_S2=abc\nESPN_SWID={D}\n")
    assert load_dotenv(path) == {"ESPN_S2": "abc", "ESPN_SWID": "{D}"}


def test_dotenv_strips_matched_quotes():
    """Values get pasted with quotes constantly; keeping them 401s."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text('ESPN_S2="abc"\nESPN_SWID=\'{D}\'\n', encoding="utf-8")
        assert load_dotenv(path) == {"ESPN_S2": "abc", "ESPN_SWID": "{D}"}


def test_a_missing_dotenv_is_not_an_error():
    """Plenty of setups pass real environment variables instead."""
    from pathlib import Path

    assert load_dotenv(Path("does-not-exist-anywhere")) == {}


def test_a_malformed_line_says_which_line(tmp_path):
    path = write_env(tmp_path, "ESPN_S2=abc\nthis is not a pair\n")
    with pytest.raises(EspnApiError, match=":2:"):
        load_dotenv(path)


def test_real_environment_variables_beat_the_file(tmp_path, monkeypatch):
    path = write_env(tmp_path, "ESPN_S2=from_file\nESPN_SWID={FILE}\n")
    monkeypatch.setenv("ESPN_S2", "from_env")

    creds = load_credentials(path)
    assert creds is not None
    assert creds.espn_s2 == "from_env"


def test_missing_credentials_are_only_an_error_when_required(tmp_path):
    path = write_env(tmp_path, "ESPN_S2=\nESPN_SWID=\n")
    assert load_credentials(path) is None
    with pytest.raises(EspnApiError, match="ESPN_S2 and ESPN_SWID"):
        load_credentials(path, required=True)


def test_a_swid_pasted_without_braces_gets_them_back():
    """ESPN rejects a bare SWID, and the rejection looks like bad credentials."""
    cookies = EspnCredentials(espn_s2="s", swid="ABC-DEF").as_cookies()
    assert cookies["SWID"] == "{ABC-DEF}"


def test_a_swid_that_already_has_braces_is_left_alone():
    cookies = EspnCredentials(espn_s2="s", swid="{ABC-DEF}").as_cookies()
    assert cookies["SWID"] == "{ABC-DEF}"


def test_surrounding_whitespace_is_stripped_from_both_cookies():
    cookies = EspnCredentials(espn_s2="  s2  ", swid="  {A}  ").as_cookies()
    assert cookies == {"espn_s2": "s2", "SWID": "{A}"}


@pytest.mark.parametrize("raw", ["{abc-def}", "ABC-DEF", " {ABC-DEF} ", "abc-def"])
def test_owner_ids_fold_across_braces_and_case(raw):
    """The same id is written four ways across ESPN's own payloads.

    A fold that misses any of them silently fails to match a real owner, and
    the symptom — "we couldn't work out which team is yours" — points nowhere
    near the cause.
    """
    assert fold_owner_id(raw) == "ABC-DEF"


def test_folding_nothing_gives_nothing():
    assert fold_owner_id(None) == ""
    assert fold_owner_id("  ") == ""
