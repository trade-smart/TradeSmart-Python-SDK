"""The suite runs in parallel (pytest-xdist) by default; the opt-in live suites always run serially.

The live order cap and "one open order at a time" rule live in one per-process session, so a
parallel or looponfail run would multiply them: a live opt-in variable forces a serial run
(``tests/conftest.py``) and refuses ``-f/--looponfail``; a live order test inside an xdist worker
refuses to run at all (``tests/live_orders/safety.py``).

The end-to-end tests run ``pytest`` in a subprocess with a probe plugin that records, for every
test, the ``PYTEST_XDIST_WORKER`` it ran under (``main`` when it ran in the controller), so they
observe where tests actually run instead of parsing xdist's console output.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import conftest as root_conftest
from tests.live_orders import safety

SDK_DIR = Path(__file__).resolve().parents[2]
BOTH_OPT_INS = {"TRADESMART_LIVE_TESTS": "1", "TRADESMART_LIVE_ORDER_TESTS": "1"}
PROBE_TARGET = "tests/unit/test_package.py"  # small, fast, no fixtures

PROBE_PLUGIN = textwrap.dedent(
    """
    import os


    def pytest_runtest_setup(item):
        with open(os.environ["WORKER_PROBE_FILE"], "a", encoding="utf-8") as fh:
            fh.write(os.environ.get("PYTEST_XDIST_WORKER", "main") + "\\n")
    """
)


# -- configuration text (pins pyproject.toml against accidental edits; behaviour is below) ----


def _addopts() -> str:
    text = (SDK_DIR / "pyproject.toml").read_text(encoding="utf-8")
    (line,) = [ln for ln in text.splitlines() if ln.startswith("addopts")]
    return line


def test_pyproject_addopts_text_has_four_workers_one_file_per_worker() -> None:
    addopts = _addopts()
    assert "-n 4" in addopts  # Docker Desktop is limited to 4 CPUs
    assert "--dist loadfile" in addopts  # a file's fixtures and servers stay on one worker
    for kept in ("-ra", "--strict-markers", "not live_orders"):
        assert kept in addopts


def test_pyproject_lists_pytest_xdist_as_a_dev_dependency() -> None:
    text = (SDK_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pytest-xdist>=3.6",' in text


# -- the live-order gate (second line of defence) -------------------------------------------


def test_live_order_gate_stays_closed_inside_an_xdist_worker() -> None:
    reason = safety.gate_skip_reason({**BOTH_OPT_INS, "PYTEST_XDIST_WORKER": "gw0"})
    assert reason is not None
    assert "serial" in reason


def test_without_the_opt_ins_a_worker_reports_the_opt_in_reason() -> None:
    reason = safety.gate_skip_reason({"PYTEST_XDIST_WORKER": "gw0"})
    assert reason is not None
    assert "opt-in" in reason


# -- the serial guard: which values count as an opt-in --------------------------------------


def _config(**option: Any) -> Any:
    namespace = SimpleNamespace(**option)
    return SimpleNamespace(
        option=namespace, getoption=lambda name, default=None: getattr(namespace, name, default)
    )


@pytest.mark.parametrize("value", ["0", "", "true"])
def test_only_the_exact_value_1_is_an_opt_in(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_TESTS", value)
    monkeypatch.delenv("TRADESMART_LIVE_ORDER_TESTS", raising=False)
    config = _config(numprocesses=4, dist="loadfile", tx=["popen"] * 4, looponfail=True)
    root_conftest.pytest_cmdline_main(config)  # no usage error, nothing changed
    assert (config.option.numprocesses, config.option.dist) == (4, "loadfile")


# -- end to end: where the tests of a real `pytest` run execute -----------------------------


def _run_probe(
    tmp_path: Path, args: list[str], opt_ins: dict[str, str], timeout: float = 300
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir(exist_ok=True)
    (probe_dir / "worker_probe.py").write_text(PROBE_PLUGIN, encoding="utf-8")
    record = tmp_path / "workers.txt"
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTEST_", "TRADESMART_", "COV_CORE_"))
    }
    env.update(
        opt_ins,
        TRADESMART_TOKEN_FILE=str(tmp_path / "token.json"),
        PYTEST_ADDOPTS="-p no:cacheprovider",
        PYTHONPATH=os.pathsep.join(filter(None, [str(probe_dir), env.get("PYTHONPATH")])),
        WORKER_PROBE_FILE=str(record),
    )
    result = subprocess.run(  # noqa: S603 - fixed arguments
        [sys.executable, "-m", "pytest", PROBE_TARGET, "-p", "worker_probe", *args],
        cwd=SDK_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    ran_in = record.read_text(encoding="utf-8").split() if record.exists() else []
    return result, ran_in


def test_control_without_an_opt_in_the_tests_run_in_xdist_workers(tmp_path: Path) -> None:
    """Keeps the serial checks below meaningful: the same command does start workers."""
    result, ran_in = _run_probe(tmp_path, [], {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert ran_in, "no test ran"
    assert all(name.startswith("gw") for name in ran_in), ran_in


@pytest.mark.parametrize("opt_in", sorted(BOTH_OPT_INS))
def test_either_live_opt_in_runs_every_test_in_the_main_process(
    opt_in: str, tmp_path: Path
) -> None:
    result, ran_in = _run_probe(tmp_path, [], {opt_in: "1"})  # `-n 4` from addopts
    assert result.returncode == 0, result.stdout + result.stderr
    assert ran_in, "no test ran"
    assert set(ran_in) == {"main"}, ran_in


# Every xdist option that starts worker processes, plus --pdb (allowed once the run is serial)
# and a run without xdist at all (`-p no:xdist` alone is a usage error with `-n 4` in addopts).
SPAWNING_ARGS = {
    "n-2": ["-n", "2"],
    "n-auto": ["-n", "auto"],
    "tx-dist-load": ["--tx", "2*popen", "--dist", "load"],
    "d-tx": ["-d", "--tx", "popen"],
    "worksteal": ["-n", "2", "--dist", "worksteal"],
    "pdb": ["--pdb"],
    "no-xdist": ["-o", "addopts=", "-p", "no:xdist"],
}


@pytest.mark.parametrize("args", list(SPAWNING_ARGS.values()), ids=list(SPAWNING_ARGS))
def test_a_live_opt_in_overrides_every_option_that_starts_workers(
    args: list[str], tmp_path: Path
) -> None:
    result, ran_in = _run_probe(tmp_path, args, {"TRADESMART_LIVE_TESTS": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert ran_in, "no test ran"
    assert set(ran_in) == {"main"}, ran_in


@pytest.mark.parametrize("flag", ["-f", "--looponfail"])
def test_looponfail_with_a_live_opt_in_is_a_usage_error_and_runs_nothing(
    flag: str, tmp_path: Path
) -> None:
    # Without the guard looponfail loops until interrupted: the timeout turns that into a failure.
    result, ran_in = _run_probe(tmp_path, [flag], {"TRADESMART_LIVE_TESTS": "1"})
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "looponfail" in result.stderr
    assert ran_in == []
