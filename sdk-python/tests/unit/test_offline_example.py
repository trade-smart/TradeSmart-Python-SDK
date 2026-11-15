"""The offline testing guide and its example: the example's tests pass, the page opens short."""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path
from types import ModuleType

import pytest

SDK_DIR = Path(__file__).resolve().parents[2]
EXAMPLE = SDK_DIR / "examples" / "offline_strategy_test.py"
PAGE = SDK_DIR / "docs" / "testing.md"
_FIRST_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_example_offline_strategy_test", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _example_tests() -> list[str]:
    source = EXAMPLE.read_text(encoding="utf-8") if EXAMPLE.is_file() else ""
    return re.findall(r"^def (test_\w+)\(\)", source, re.MULTILINE)


def test_example_has_tests() -> None:
    assert len(_example_tests()) >= 4, f"{EXAMPLE.name} has too few test functions"


@pytest.mark.parametrize("name", _example_tests() or ["<missing>"])
def test_example_test_passes(name: str) -> None:
    """Each ``test_*`` function of the example runs in-process and passes."""
    module = _load_example()
    test = getattr(module, name)
    assert not inspect.iscoroutinefunction(test), "example tests need no async plugin"
    test()


def test_example_runs_every_test_as_a_script(capsys: pytest.CaptureFixture[str]) -> None:
    """``python offline_strategy_test.py`` runs every test and says how many passed."""
    module = _load_example()
    assert module.main() == 0
    out = capsys.readouterr().out
    assert f"{len(_example_tests())} passed" in out


def test_testing_page_starts_with_one_screen_example() -> None:
    text = PAGE.read_text(encoding="utf-8")
    first_section = text.find("\n## ")
    match = _FIRST_BLOCK.search(text)
    assert match is not None, "testing.md has no python example"
    assert first_section == -1 or match.start() < first_section, (
        "the first example must come before the first section"
    )
    lines = match.group(1).rstrip("\n").splitlines()
    assert len(lines) <= 25, f"the first example has {len(lines)} lines (at most 25)"


def test_testing_page_sections_are_short() -> None:
    """Each look-up section's examples are at most ~5 lines (spec: one screen, then look-ups)."""
    text = PAGE.read_text(encoding="utf-8")
    body = text[text.find("\n## ") :]
    for block in _FIRST_BLOCK.findall(body):
        lines = block.rstrip("\n").splitlines()
        assert len(lines) <= 6, f"a look-up example has {len(lines)} lines:\n{block}"


@pytest.mark.parametrize(
    "needle",
    [
        "max_retries=0",  # a drill on a read is retried by the SDK
        'pytest_plugins = ["tradesmart.testing.pytest_plugin"]',
        "set_reply",  # PlaceMultiOrder and other endpoints the offline broker does not model
        "instruments_dir",
        "~/.cache/tradesmart",
        "OrderStatusUnknownError",
        "timeout_after_send",
        "drop_stream",
        "refuse_stream_login",
        "silence_stream",
        "default_place",
        "sandbox",
        "offline_strategy_test.py",
        "10 orders per second",  # the SDK's own order rate limit still applies
        "or call `broker.close()`",  # never rely on garbage collection to stop the server
        "keeps it alive",  # a client or stream keeps its broker alive (round-1 I4)
        "newest first, as the API sends them",  # candles (round-1 I1)
        "json=True",  # a 5xx without JSON is an unknown order outcome (round-1 I2)
        "cover or bracket order",  # ExitSNOOrder (round-1 I3)
        "for 24 hours",  # the synthetic login (round-1 M1)
        "Quote fields you do\nnot set are 0",  # round-1 M3
        "200 per minute",  # the order rate limit's minute window (round-1 M4)
    ],
)
def test_testing_page_states(needle: str) -> None:
    assert needle in PAGE.read_text(encoding="utf-8")


def test_guide_index_links_the_testing_page() -> None:
    text = (PAGE.parent / "README.md").read_text(encoding="utf-8")
    assert "](testing.md)" in text


def test_sdk_readme_links_the_testing_page_and_example() -> None:
    text = (SDK_DIR / "README.md").read_text(encoding="utf-8")
    github = "https://github.com/trade-smart/TradeSmart-Python-SDK/blob/main/"
    assert f"{github}sdk-python/docs/testing.md" in text
    assert f"{github}sdk-python/examples/offline_strategy_test.py" in text


def test_changelog_adds_the_offline_broker_in_2_0_0() -> None:
    text = (SDK_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    release = text[text.index("## [2.0.0]") : text.index("## [0.2.0]")]
    start = release.index("### Added")
    added = release[start : release.index("\n### ", start + 1)]
    assert "tradesmart.testing" in added and "OfflineBroker" in added


def test_behaviour_spec_has_the_optional_offline_broker_section() -> None:
    text = (SDK_DIR.parent / "specifications" / "sdk-behaviour.md").read_text(encoding="utf-8")
    assert "Offline broker (optional)" in text


def test_testing_page_quick_start_runs_as_is() -> None:
    """The opening example is copy-and-run: its test functions pass."""
    match = _FIRST_BLOCK.search(PAGE.read_text(encoding="utf-8"))
    assert match is not None
    namespace: dict[str, object] = {"__name__": "quick_start"}
    exec(compile(match.group(1), "testing.md quick start", "exec"), namespace)  # noqa: S102
    tests = [value for name, value in namespace.items() if name.startswith("test_")]
    assert tests, "the quick start has no test function"
    for test in tests:
        assert callable(test)
        test()


def test_example_runner_reports_any_exception_as_a_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mis-driven broker (``ValueError``) is a failed test, not an aborted run."""
    module = _load_example()

    def test_broken() -> None:
        raise ValueError("no order with id 'nope'")

    monkeypatch.setattr(module, "TESTS", [test_broken])
    assert module.main() == 1
    out = capsys.readouterr().out
    assert "FAIL test_broken: ValueError" in out
    assert "0 passed, 1 failed" in out


def test_testing_page_lists_the_no_quote_assumption() -> None:
    text = PAGE.read_text(encoding="utf-8")
    assumed = text[text.index("## Assumed shapes") :]
    assumed = assumed[: assumed.index("\n## ", 1)]
    assert "set_quote" in assumed and "no data" in assumed
    assert "like a real reply" not in text
    # Documented behaviour is not "assumed"; undocumented shapes the offline broker picked are.
    assert "in the order you set them" not in text
    assert "ExitSNOOrder" in assumed and "429" in assumed
