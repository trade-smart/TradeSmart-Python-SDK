"""The "open an account" signup link: one tagged URL per place, in the CLI and the docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tradesmart import _cli
from tradesmart._links import SIGNUP_PLACES, signup_url

SDK_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = SDK_DIR.parent

PLACEMENTS = [
    (REPO_DIR / "README.md", "readme"),
    (SDK_DIR / "README.md", "pypi"),
    (SDK_DIR / "docs" / "getting-started.md", "getting_started"),
    (REPO_DIR / "docs" / "api-access.md", "api_access"),
    (SDK_DIR / "pyproject.toml", "pypi_sidebar"),
]
SIGNUP_URL_RE = re.compile(r"https://signup\.tradesmartonline\.in/[^\s)\"'>]*")


def test_signup_url_has_sdk_tags() -> None:
    assert signup_url("cli") == (
        "https://signup.tradesmartonline.in/?utm_source=PythonSDK&utm_medium=Organic"
        "&utm_campaign=SDK_Open_Demat&utm_content=cli&channel=Organic&platform=web"
    )


def test_signup_places_are_the_documented_ones() -> None:
    assert (
        frozenset(
            {
                "readme",
                "pypi",
                "getting_started",
                "api_access",
                "pypi_sidebar",
                "cli",
                "github_about",
            }
        )
        == SIGNUP_PLACES
    )


def test_signup_url_rejects_unknown_place() -> None:
    with pytest.raises(ValueError, match="readmee"):
        signup_url("readmee")


@pytest.mark.parametrize(("path", "place"), PLACEMENTS, ids=[p for _, p in PLACEMENTS])
def test_docs_carry_signup_link(path: Path, place: str) -> None:
    assert signup_url(place) in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("path", "place"), PLACEMENTS, ids=[p for _, p in PLACEMENTS])
def test_no_untagged_signup_links(path: Path, place: str) -> None:
    found = SIGNUP_URL_RE.findall(path.read_text(encoding="utf-8"))
    assert found
    assert set(found) == {signup_url(place)}


def _eof(prompt: str = "") -> str:
    raise EOFError


@pytest.fixture
def no_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TRADESMART_CLIENT_ID", "TRADESMART_APP_KEY", "TRADESMART_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_first_login_prompt_shows_signup_hint_once_before_asking(
    no_creds: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hint = f"No TradeSmart account yet? Open one at {signup_url('cli')}"
    seen: list[str] = []

    def ask(prompt: str = "") -> str:
        seen.append(capsys.readouterr().out)  # what was printed before the prompt
        raise EOFError

    monkeypatch.setattr("builtins.input", ask)
    assert _cli.main(["login", "--no-browser"]) == _cli.EXIT_USAGE
    assert len(seen) == 1
    assert seen[0].count(hint) == 1


@pytest.mark.parametrize("source", ["flag", "env", "blank_env"])
def test_login_without_client_id_prompt_has_no_hint(
    source: str,
    no_creds: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["login", "--no-browser"]
    if source == "flag":
        argv += ["--client-id", "TEST123"]
    else:
        monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123" if source == "env" else "   ")
    monkeypatch.setattr("builtins.input", _eof)
    monkeypatch.setattr(_cli.getpass, "getpass", _eof)
    assert _cli.main(argv) == _cli.EXIT_USAGE  # stopped at the keys (or the blank id), not earlier
    out = capsys.readouterr()
    assert "signup.tradesmartonline.in" not in out.out + out.err
    expected = "client id must not be empty" if source == "blank_env" else "app key"
    assert expected in out.err.lower()


def test_returning_user_prompt_has_no_hint(
    no_creds: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = _cli._token_store.token_path()  # an empty temp folder (tests/conftest.py)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("builtins.input", _eof)
    assert _cli.main(["login", "--no-browser"]) == _cli.EXIT_USAGE
    assert "signup.tradesmartonline.in" not in capsys.readouterr().out
