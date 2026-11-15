"""The PyPI release workflow: a GitHub Release (or a manual run) builds, checks and uploads.

Text checks only (no YAML dependency). The upload uses PyPI Trusted Publishing behind the protected
``pypi`` environment, so a person approves every upload and no PyPI token is stored anywhere.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(text: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, flags=re.M | re.S)
    assert match, f"release.yml has no {name} job"
    return match.group(1)


def test_runs_on_a_published_release_or_by_hand() -> None:
    text = _text()
    assert re.search(r"release:\n\s+types: \[published\]", text)
    assert "workflow_dispatch:" in text


def test_build_checks_the_tag_against_the_package_version() -> None:
    build = _job(_text(), "build")
    assert "src/tradesmart/_version.py" in build
    assert "exit 1" in build
    assert "python -m build" in build
    assert "twine check dist/*" in build


def test_publish_uses_trusted_publishing_behind_an_approval() -> None:
    text = _text()
    publish = _job(text, "publish")
    assert "needs: build" in publish
    assert re.search(r"environment:\n\s+name: pypi\n\s+url: https://pypi.org/p/tradesmart", publish)
    assert "id-token: write" in publish
    assert "pypa/gh-action-pypi-publish@release/v1" in publish
    # Only the publish job may mint an OIDC token; the default stays read-only.
    assert re.search(r"^permissions:\n  contents: read", text, flags=re.M)
    assert "id-token" not in _job(text, "build")


def test_no_stored_pypi_token() -> None:
    text = _text()
    for word in ("password:", "PYPI_API_TOKEN", "secrets."):
        assert word not in text, word
