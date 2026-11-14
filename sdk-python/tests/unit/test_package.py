import re
from pathlib import Path

import tradesmart
from tests._public_tree import names_internal_material

SDK_DIR = Path(__file__).resolve().parents[2]


def test_version() -> None:
    assert tradesmart.__version__ == "2.0.0"


def test_shipped_code_has_no_internal_references() -> None:
    """Planning documents are not published: shipped code must not point at them."""
    package = Path(tradesmart.__file__).parent
    offenders = [
        f"{path.relative_to(package)}:{number}"
        for path in sorted(package.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if names_internal_material(line)
    ]
    assert offenders == []


def test_sdist_ships_a_public_gitignore() -> None:
    """Hatchling always puts the nearest ``.gitignore`` (searching up from the project folder)
    into the sdist. ``sdk-python/.gitignore`` is that file, so the repository root's ignore
    rules, which name local workflow folders, never reach the package."""
    ignore = SDK_DIR / ".gitignore"
    assert ignore.is_file()
    lines = ignore.read_text(encoding="utf-8").splitlines()
    offenders = [ln for ln in lines if names_internal_material(ln) or "claude" in ln.lower()]
    assert offenders == []
    assert {".live-results/", ".env", "dist/"} <= set(lines)


def test_sdist_includes_are_anchored_at_the_project_folder() -> None:
    """A bare ``README.md`` pattern matches at any depth (the docs' READMEs too)."""
    text = (SDK_DIR / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.hatch.build.targets.sdist]", 1)[1].split("\n[", 1)[0]
    match = re.search(r"^include = \[(.*)\]$", section, re.M)
    assert match is not None
    patterns = re.findall(r'"([^"]+)"', match.group(1))
    assert patterns == [
        "/src",
        "/README.md",
        "/CHANGELOG.md",
        "/LICENSE",
        "/NOTICE",
        "/pyproject.toml",
    ]
