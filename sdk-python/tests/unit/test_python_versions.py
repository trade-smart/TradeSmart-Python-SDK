"""Supported Python versions: the floor is 3.11 and every newer release up to 3.15 is tested.

One list, checked against every place that names a version: the package metadata, the lint and
type-check targets, the Docker services and the GitHub workflow.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

SDK = Path(__file__).resolve().parents[2]
REPO = SDK.parent
FLOOR = (3, 11)
SUPPORTED = ["3.11", "3.12", "3.13", "3.14", "3.15"]


def _pyproject() -> dict[str, object]:
    with (SDK / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _compose_versions() -> dict[str, str]:
    """``service name -> PYTHON_VERSION`` from compose.yaml (text check; no YAML dependency)."""
    versions: dict[str, str] = {}
    service = ""
    for line in (SDK / "compose.yaml").read_text(encoding="utf-8").splitlines():
        header = re.fullmatch(r"  ([a-z0-9_-]+):", line)
        if header:
            service = header.group(1)
        found = re.fullmatch(r'\s+PYTHON_VERSION: "([\d.]+)"', line)
        if found and service:
            versions[service] = found.group(1)
    return versions


def test_running_python_is_at_least_the_floor() -> None:
    assert sys.version_info >= FLOOR


def test_requires_python_is_the_floor() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11"


def test_classifiers_list_exactly_the_supported_versions() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    prefix = "Programming Language :: Python :: 3."
    listed = [c.rsplit(" :: ", 1)[1] for c in project["classifiers"] if c.startswith(prefix)]
    assert listed == SUPPORTED


def test_lint_and_type_check_target_the_floor() -> None:
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    assert tool["ruff"]["target-version"] == "py311"
    assert tool["mypy"]["python_version"] == "3.11"


def test_docker_services_cover_every_supported_version() -> None:
    versions = _compose_versions()
    assert versions["dev"] == "3.12"
    assert versions["login"] == "3.12"
    for version in SUPPORTED:
        if version == "3.12":
            continue
        assert versions[f"py{version.replace('.', '')}"] == version
    assert sorted(set(versions.values())) == SUPPORTED
    assert "py39" not in versions


def test_github_workflow_tests_every_supported_version() -> None:
    workflow = (REPO / ".github" / "workflows" / "python-ci.yml").read_text(encoding="utf-8")
    matrix = re.search(r"python-version: \[([^\]]*)\]", workflow)
    assert matrix is not None
    assert [v.strip().strip('"') for v in matrix.group(1).split(",")] == SUPPORTED


def test_github_workflow_tests_every_supported_operating_system() -> None:
    # The SDK supports Linux, Windows and macOS (token file location and permissions differ).
    workflow = (REPO / ".github" / "workflows" / "python-ci.yml").read_text(encoding="utf-8")
    for os_label in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert os_label in workflow, os_label
