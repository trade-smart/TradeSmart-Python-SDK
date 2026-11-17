"""The PyPI install check: installs the published package from PyPI, like a user, and imports it.

Text checks only (no YAML dependency). The workflow never checks out the repository, so it can only
pass when the copy on PyPI installs and works.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "install-check.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _versions(text: str) -> list[str]:
    match = re.search(r"python-version: \[([^\]]*)\]", text)
    assert match is not None
    return [v.strip().strip('"') for v in match.group(1).split(",")]


def test_runs_by_hand_for_any_version_and_when_the_file_changes() -> None:
    text = _text()
    assert re.search(r"workflow_dispatch:\n\s+inputs:\n\s+version:", text)
    push = r"push:\n\s+branches: \[main\]\n\s+paths:\n\s+- \"\.github/workflows/install-check"
    assert re.search(push, text)


def test_installs_from_pypi_without_the_repository() -> None:
    text = _text()
    assert "actions/checkout" not in text, "a checkout would let the local source mask PyPI"
    assert 'pip install --no-cache-dir "$spec"' in text
    assert 'spec="tradesmart==$VERSION"' in text
    assert "pip check" in text
    assert "site-packages" in text, "the smoke test must prove the import is the installed copy"
    assert "python -m tradesmart --help" in text


def test_covers_every_supported_python_and_windows_but_not_macos() -> None:
    text = _text()
    ci = (WORKFLOWS / "python-ci.yml").read_text(encoding="utf-8")
    assert _versions(text) == _versions(ci)
    assert "windows-latest" in text
    assert "macos" not in text, "macOS is covered by the Python CI on every push"


def test_read_only_and_no_secrets() -> None:
    text = _text()
    assert re.search(r"^permissions:\n  contents: read", text, flags=re.M)
    assert "secrets." not in text
