"""Guards for the public monorepo layout: each SDK is self-contained, shared docs are neutral."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests._public_tree import (
    OPEN_ITEM_TABLE,
    defined_open_items,
    internal_references,
    is_exempt,
    is_exported,
    is_internal_only_test,
    is_public,
    names_internal_material,
    normalise,
    open_item_ids,
    other_broker_names,
    process_ids,
    public_files,
    read_text,
    scan,
    undefined_open_items,
)

REPO_DIR = Path(__file__).resolve().parents[3]
SDK_DIR = REPO_DIR / "sdk-python"
SHARED_DOCS = REPO_DIR / "docs"

PYTHON_DOCS = [
    "README.md",
    "getting-started.md",
    "authentication.md",
    "orders.md",
    "streaming.md",
    "market-data.md",
    "option-analytics.md",
    "testing.md",
    "configuration.md",
    "troubleshooting.md",
    "versioning.md",
    "architecture.md",
    "reference.md",
    "migration/README.md",
]
SHARED_DOC_FILES = ["README.md", "api-access.md"]
MOVED_AWAY = [
    "docs/python",
    "docs/migration",
    "docs/sdk-architecture.md",
    "docs/authentication.md",
    "examples",
]


def test_python_docs_live_in_sdk_python() -> None:
    missing = [p for p in PYTHON_DOCS if not (SDK_DIR / "docs" / p).is_file()]
    missing += [f"docs/{p}" for p in SHARED_DOC_FILES if not (SHARED_DOCS / p).is_file()]
    leftover = [p for p in MOVED_AWAY if (REPO_DIR / p).exists()]
    assert missing == []
    assert leftover == []


def test_root_readme_heading_is_the_product_name() -> None:
    # The owner's chosen title, shown at the top of the GitHub repository page.
    first_line = (REPO_DIR / "README.md").read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "# TradeSmart Python SDK"


def test_root_readme_is_a_short_python_landing_page() -> None:
    # One repository per language: no table of planned SDKs, and short enough to read at a glance.
    text = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 60
    assert "Planned" not in text and "| SDK |" not in text
    assert "pip install tradesmart" in text and "python -m tradesmart login" in text
    assert "https://signup.tradesmartonline.in/?utm_source=PythonSDK" in text
    for link in (
        "sdk-python/docs/getting-started.md",
        "sdk-python/docs/README.md",
        "sdk-python/docs/reference.md",
    ):
        assert f"]({link})" in text, link


# --- language-neutral shared docs ---------------------------------------------------------------

_PYTHON_FENCE = re.compile(r"^\s*(```|~~~)\s*(python3?|py|pycon|ipython)\b", re.I | re.M)
_PYTHON_NEEDLES = (
    re.compile(r"\bimport tradesmart\b"),
    re.compile(r"\bfrom tradesmart\b"),
    re.compile(r"\bpython3? -m\b"),
    re.compile(r"\bpip3? install\b"),
    re.compile(r"\b[\w/.-]+\.py\b"),
)


def test_shared_docs_are_language_neutral() -> None:
    """The root ``docs/`` folder serves every SDK: language-specific content lives in each SDK."""
    hits = []
    for doc in sorted(SHARED_DOCS.rglob("*.md")):
        text = doc.read_text(encoding="utf-8")
        where = doc.relative_to(REPO_DIR).as_posix()
        if _PYTHON_FENCE.search(text):
            hits.append(f"{where}: Python code fence")
        hits += [f"{where}: {n.pattern}" for n in _PYTHON_NEEDLES if n.search(text)]
    assert hits == []


# --- public markdown links ----------------------------------------------------------------------

REPO_URL = "https://github.com/trade-smart/TradeSmart-Python-SDK"
_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_LINK = re.compile(
    r"!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'(][^)]*[\"')])?\s*\)"
)
_REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
_HREF = re.compile(r"""\b(?:href|src)=["']([^"']+)["']""")
_CODE_SPAN = re.compile(r"(`+).+?\1")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")


def _prose_lines(text: str) -> list[str]:
    """Lines outside fenced code blocks, with inline code spans blanked out."""
    out, fenced = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(_CODE_SPAN.sub("", line))
    return out


def _links(text: str) -> list[str]:
    found: list[str] = []
    for line in _prose_lines(text):
        found += _INLINE_LINK.findall(line) + _HREF.findall(line)
        ref = _REF_DEF.match(line)
        if ref:
            found.append(ref.group(1))
    return found


def _anchors(path: Path) -> set[str]:
    """GitHub heading slugs of a markdown file (duplicates get ``-1``, ``-2``)."""
    seen: dict[str, int] = {}
    slugs = set()
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        match = _HEADING.match(line)
        if not match:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1))
        slug = re.sub(r"[^\w\- ]", "", title.lower().replace("`", "")).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        slugs.add(slug if count == 0 else f"{slug}-{count}")
    return slugs


def _resolve(source: Path, link: str) -> str | None:
    """Problem with ``link`` found in ``source`` (``None`` when it resolves to a public target)."""
    if link.startswith(("mailto:", "tel:")):
        return None
    if link.startswith(("https://", "http://")):
        if not link.startswith(REPO_URL + "/"):
            return None  # external sites are not checked offline
        kind, _, rest = link[len(REPO_URL) + 1 :].partition("/")
        if kind not in ("blob", "tree"):
            return None  # issues, pulls, releases, ...
        branch, _, rest = rest.partition("/")
        if branch != "main":
            return f"{link}: not the main branch"
        target_part, _, fragment = rest.partition("#")
        target = REPO_DIR / target_part
        if kind == "blob" and target.is_dir():
            return f"{link}: blob URL names a folder"
        if kind == "tree" and target.is_file():
            return f"{link}: tree URL names a file"
    else:
        path_part, _, fragment = unquote(link).partition("#")
        path_part = path_part.partition("?")[0]
        target = source if not path_part else (source.parent / path_part).resolve()
        if REPO_DIR not in (target, *target.parents):
            return f"{link}: leaves the repository"
    rel = target.relative_to(REPO_DIR).as_posix() if target != REPO_DIR else "."
    if rel != "." and not is_public(rel):
        return f"{link}: points at internal-only material"
    if not target.exists():
        return f"{link}: no such file or folder"
    if fragment and target.suffix == ".md" and fragment.lower() not in _anchors(target):
        return f"{link}: no heading for the anchor"
    return None


def test_every_public_markdown_link_resolves() -> None:
    problems = []
    for doc in (p for p in public_files() if p.suffix == ".md"):
        for link in _links(doc.read_text(encoding="utf-8")):
            issue = _resolve(doc, link)
            if issue:
                problems.append(f"{doc.relative_to(REPO_DIR).as_posix()}: {issue}")
    assert problems == []


def test_link_checker_catches_broken_links() -> None:
    """Negative control: the checker is not vacuous."""
    source = SDK_DIR / "README.md"
    assert _resolve(source, "docs/does-not-exist.md")
    assert _resolve(source, "docs/README.md#no-such-heading")
    assert _resolve(source, f"{REPO_URL}/blob/main/sdk-python/docs")
    assert _resolve(source, f"{REPO_URL}/tree/main/sdk-python/README.md")
    assert _resolve(source, f"{REPO_URL}/blob/main/docs/python/README.md")
    assert _resolve(source, "docs/README.md") is None
    assert _links("a [x](y.md#z) `[no](code.md)`\n```\n[no](fence.md)\n```\n") == ["y.md#z"]


@pytest.mark.parametrize(
    "link",
    [
        "../CLAUDE.md",
        "../internal",
        "../internal/",
        "../internal/plot.md",
        "../.claude",
        "../.superpowers/x.md",
        f"{REPO_URL}/blob/main/CLAUDE.md",
        f"{REPO_URL}/tree/main/internal",
    ],
)
def test_link_checker_flags_links_to_internal_material(link: str) -> None:
    """Negative control: a link may exist in the internal checkout and still not be public."""
    problem = _resolve(SDK_DIR / "README.md", link)
    assert problem is not None and "internal-only" in problem


def test_pypi_readme_links_are_absolute() -> None:
    """PyPI renders ``sdk-python/README.md`` away from the repository: no relative links."""
    relative = [
        link
        for link in _links((SDK_DIR / "README.md").read_text(encoding="utf-8"))
        if not link.startswith(("https://", "http://", "#", "mailto:"))
    ]
    assert relative == []


# --- internal references ------------------------------------------------------------------------


def test_no_public_file_references_internal_material() -> None:
    offenders = []
    for path in public_files():
        rel = path.relative_to(REPO_DIR).as_posix()
        text = read_text(path)
        found = scan(rel, text) if text is not None else []
        if found:
            offenders.append(f"{rel}: {found}")
    assert offenders == []


def test_no_public_file_names_internal_process_ids() -> None:
    """Review-ruling ids mean nothing in the public repository: no public file, exempt or not,
    may cite one (only the internal-only tests, which are not exported)."""
    offenders = []
    for path in public_files():
        rel = path.relative_to(REPO_DIR).as_posix()
        if is_internal_only_test(rel):
            continue
        found = process_ids(read_text(path) or "")
        if found:
            offenders.append(f"{rel}: {found}")
    assert offenders == []


@pytest.mark.parametrize(
    "text", ["see rul" + "ing L3", "Rul" + "ing R10: streams", "RUL" + "ING L12"]
)
def test_process_id_pattern_catches(text: str) -> None:
    assert process_ids(text)


@pytest.mark.parametrize("text", ["a ruling on the case", "the ruling party", "ruler L3"])
def test_process_id_pattern_ignores_prose(text: str) -> None:
    assert process_ids(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "see internal/plot.md",
        "src/internal/x",
        "see PLOT.md",
        "plot.md",
        "master_prompt",
        "Master_Prompt.md",
        "[a](internal)",
        "[a](../internal)",
        "`internal`",
        "open CLAUDE.md",
        "claude.md",
        "the .claude/settings",
        ".superpowers/sdd",
        "docs/superpowers/specs",
    ],
)
def test_internal_reference_patterns_catch(text: str) -> None:
    assert internal_references(text)


@pytest.mark.parametrize(
    "text",
    ["an internal preview, not published", "internal API", "Internal use", "see claude.com"],
)
def test_internal_reference_patterns_ignore_prose(text: str) -> None:
    assert internal_references(text) == []


def test_is_public_rules() -> None:
    public = [
        "README.md",
        "sdk-python/src/tradesmart/internal/mod.py",  # only the root `internal/` is internal
        "sdk-python/docs/internal/x.md",
        "docs/api-access.md",
        ".gitignore",
        "sdk-python/tests/unit/test_repo_layout.py",
    ]
    private = [
        "CLAUDE.md",
        "internal",
        "internal/plot.md",
        ".claude/worktrees/x/a.py",
        ".superpowers/sdd/x.md",
        ".worktrees",
        ".worktrees/gtt/sdk-python/src/tradesmart/client.py",
        ".live-results/a.json",
        "sdk-python/.live-results/auth.jsonl",  # the live suites write here
        "sdk-go/.Live-Results/x.json",
        ".git",
        "sdk-python/.git",
        "sdk-python/dist/x.whl",
        "docs/build/x.md",
        "sdk-python/src/tradesmart.egg-info/PKG-INFO",
        "sdk-python/.mypy_cache/a",
        "sdk-python/__pycache__/a.pyc",
        ".env",
        ".env.local",
        "sdk-python/.coverage",
        ".tox/py/x",
        ".idea/a.xml",
        "sdk-python/htmlcov/index.html",
    ]
    assert [p for p in public if not is_public(p)] == []
    assert [p for p in private if is_public(p)] == []


def test_public_files_skips_git_worktree_copies(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Public\n", encoding="utf-8")
    copy = tmp_path / ".worktrees" / "x" / "docs"
    copy.mkdir(parents=True)
    (copy / "a.md").write_text("see internal/plot.md and [x](missing.md)\n", encoding="utf-8")
    found = [p.relative_to(tmp_path).as_posix() for p in public_files(tmp_path)]
    assert found == ["README.md"]


def test_is_exempt_rules() -> None:
    assert is_exempt(".gitignore")
    assert is_exempt("sdk-python/tests/unit/test_internal_layout.py")
    assert is_exempt("sdk-python/tests/test_internal_x.py")  # directly under tests/
    assert is_exempt("sdk-python/tests/_public_tree.py")
    assert not is_exempt("sdk-python/tests/unit/test_package.py")
    assert not is_exempt("sdk-python/src/tradesmart/test_internal_x.py")
    assert scan("sdk-python/tests/unit/test_internal_x.py", "plot.md") == []
    assert scan("README.md", "plot.md") == ["plot.md"]


def test_public_files_walks_a_given_tree(tmp_path: Path) -> None:
    for name in [
        "README.md",
        "internal/plot.md",
        "sdk-python/src/internal/mod.py",
        "sdk-python/dist/a.whl",
        ".env",
        "CLAUDE.md",
        ".git",
        "docs/a.md",
    ]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    found = [p.relative_to(tmp_path).as_posix() for p in public_files(tmp_path)]
    assert found == ["README.md", "docs/a.md", "sdk-python/src/internal/mod.py"]


def test_read_text_tolerates_binary_and_missing_files(tmp_path: Path) -> None:
    binary = tmp_path / "a.bin"
    binary.write_bytes(b"\xff\xfe\x00")
    assert read_text(binary) is None
    assert read_text(tmp_path / "missing.txt") is None


def test_public_open_item_ids_are_defined_in_the_behaviour_contract() -> None:
    """``F19``-style ids are public only where the contract's open-items table defines them."""
    table = (REPO_DIR / OPEN_ITEM_TABLE).read_text(encoding="utf-8")
    defined = defined_open_items(table)
    assert defined, "no open-item table found"
    undefined = []
    for path in public_files():
        rel = path.relative_to(REPO_DIR).as_posix()
        if not is_exempt(rel):
            undefined += undefined_open_items(rel, read_text(path) or "", defined)
    assert undefined == []


def test_open_item_scan_catches_undefined_ids() -> None:
    """Negative control, including the file types beyond markdown and Python."""
    defined = {"F1"}
    for rel in ("a.md", "a.sh", "a.json", "Dockerfile", "scripts/run.ps1"):
        assert undefined_open_items(rel, "see F99 and F1", defined) == [f"{rel}: F99"]
    assert undefined_open_items("a.md", "see F100", defined) == ["a.md: F100"]
    assert undefined_open_items("a.md", "see F1 and AF2", defined) == []
    assert undefined_open_items("a.png", "F99", defined) == []


def test_defined_open_items_reads_the_table() -> None:
    table = "| Id | Item |\n|---|---|\n| F1 | a |\n|  F23 | b |\nsee F99 in prose\n"
    assert defined_open_items(table) == {"F1", "F23"}


def test_is_exported_leaves_out_internal_only_tests() -> None:
    assert is_exported("README.md")
    assert is_exported("sdk-python/tests/unit/test_repo_layout.py")
    assert is_exported("sdk-python/src/tradesmart/test_internal_x.py")  # not a test folder
    assert not is_exported("sdk-python/tests/unit/test_internal_layout.py")
    assert not is_exported("sdk-python/tests/test_internal_x.py")
    assert not is_exported("sdk-python/tests/a/b/test_internal_x.py")
    assert not is_exported("internal/plot.md")
    assert not is_exported("CLAUDE.md")


def test_normalise_rules() -> None:
    assert normalise("./a/b.md") == "a/b.md"
    assert normalise("././a.md") == "a.md"
    assert normalise("a\\b.md") == "a/b.md"
    for bad in ("", ".", "a//b", "a/./b", "../a", "a/../b", "/a", "C:/a", "c:a", "a/"):
        assert normalise(bad) is None, bad


@pytest.mark.parametrize(
    "rel",
    [
        "INTERNAL/plot.md",
        "Internal/Plot.md",
        "./internal/plot.md",
        "internal\\plot.md",
        "claude.md",
        "Claude.MD",
        "docs/CLAUDE.md",
        "CLAUDE.local.md",
        "sdk-python/claude.local.md",
        "Sdk-Python/Tests/Test_Internal_X.py",
        "sdk-python/tests/unit/test_internal_e.PY",
        "sdk-go/tests/test_internal_g.py",
        "sdk-python//tests/unit/test_internal_x.py",
        ".Claude/settings.json",
        "sdk-python/DIST/x.whl",
        "/README.md",
        "../README.md",
    ],
)
def test_internal_paths_are_not_exported_in_any_spelling(rel: str) -> None:
    assert not is_exported(rel)
    assert not is_public(rel) or "test_internal" in rel.lower()


def test_other_broker_names_are_found_outside_the_migration_guides() -> None:
    name = "Ki" + "te"
    assert other_broker_names("docs/a.md", f"like {name} does") == [name]
    assert other_broker_names("sdk-python/docs/migration/a.md", f"from {name}") == []
    assert other_broker_names("docs/a.md", "a kitestring, upstoxx") == []  # whole words only


def test_public_tree_is_found() -> None:
    names = {p.relative_to(REPO_DIR).as_posix() for p in public_files()}
    assert {"README.md", "sdk-python/pyproject.toml", ".gitignore"} <= names
    assert not [n for n in names if not is_public(n)]
    assert "CLAUDE.md" not in names


# --- CI -----------------------------------------------------------------------------------------


def _workflow_paths(text: str, trigger: str) -> list[str]:
    block = re.search(rf"^  {trigger}:\n((?:    .*\n|\n)+)", text, re.M)
    assert block, f"python-ci.yml has no {trigger} trigger"
    paths = re.search(r"^    paths:\n((?:      - .*\n)+)", block.group(1), re.M)
    assert paths, f"{trigger} has no paths filter"
    items = [line.strip()[2:].split(" #")[0].strip() for line in paths.group(1).splitlines()]
    return [item.strip("\"'") for item in items]


@pytest.mark.parametrize("trigger", ["pull_request", "push"])
def test_ci_paths_cover_python_docs(trigger: str) -> None:
    """The guide lives in ``sdk-python/docs``; the docs tests also read specs and shared docs."""
    text = (REPO_DIR / ".github/workflows/python-ci.yml").read_text(encoding="utf-8")
    paths = _workflow_paths(text, trigger)
    for needed in ("sdk-python/**", "specifications/**", "docs/**"):
        assert needed in paths, f"{trigger} paths lack {needed}"
    # The link and docs tests also read the root Markdown files and everything under .github/.
    read_by_tests = [
        path.relative_to(REPO_DIR).as_posix()
        for path in public_files()
        if (path.parent == REPO_DIR and path.suffix == ".md")
        or path.relative_to(REPO_DIR).parts[0] == ".github"
    ]
    assert "README.md" in read_by_tests and ".github/workflows/python-ci.yml" in read_by_tests
    uncovered = [rel for rel in read_by_tests if not any(_gh_match(p, rel) for p in paths)]
    assert uncovered == [], f"{trigger} paths miss {uncovered}"


def _gh_match(pattern: str, path: str) -> bool:
    """GitHub path-filter glob: ``**`` crosses folders, ``*`` stays inside one."""
    regex = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", pattern)
    )
    return re.fullmatch(regex, path) is not None


def test_gh_match_rules() -> None:
    assert _gh_match("*.md", "README.md")
    assert not _gh_match("*.md", "docs/a.md")
    assert _gh_match(".github/**", ".github/workflows/python-ci.yml")
    assert _gh_match("sdk-python/**", "sdk-python/src/x.py")
    assert not _gh_match(".github/workflows/python-ci.yml", ".github/dependabot.yml")


def test_workflow_paths_parser() -> None:
    """Negative control: the parser reads each trigger's own list and ignores comments."""
    sample = (
        "on:\n  pull_request:\n    paths:\n      - 'a/**'  # why\n      - \"b/**\"\n"
        "  push:\n    branches: [main]\n    paths:\n      - c/**\n"
    )
    assert _workflow_paths(sample, "pull_request") == ["a/**", "b/**"]
    assert _workflow_paths(sample, "push") == ["c/**"]
    with pytest.raises(AssertionError):
        _workflow_paths(sample, "schedule")


def test_shipped_code_rule_is_stricter_than_the_public_tree_rule() -> None:
    """Negative control for ``test_package.py``: the bare word is rejected in ``src/``."""
    for line in ("# see superpowers:tdd", "see the Superpowers notes", "plot.md", "internal/x"):
        assert names_internal_material(line), line
    assert not names_internal_material("an ordinary comment")
    assert internal_references("superpowers:tdd") == []  # fine in public docs, not in src/


def test_open_item_ids_ignore_lint_codes() -> None:
    assert open_item_ids("x = 1  # noqa: E501, F401\nsee F19") == {"F19"}


def test_public_files_link_the_real_github_repository() -> None:
    # The repository is trade-smart/TradeSmart-Python-SDK; the earlier placeholder name gave broken
    # links and a failing pip install command. Built from parts so this file does not match itself.
    old = "trade-smart/" + "tradesmart-sdks"
    hits = []
    for path in sorted(REPO_DIR.rglob("*")):
        rel = path.relative_to(REPO_DIR).as_posix()
        if not path.is_file() or rel.split("/")[0] in {"internal", ".git"} or "/." in "/" + rel:
            continue
        if path.suffix not in {".md", ".toml", ".yml", ".yaml", ".py", ".txt", ""}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits += [rel for line in text.splitlines() if old in line.lower()]
    assert hits == []


def test_ci_actions_run_on_a_supported_node() -> None:
    # GitHub deprecated Node 20 for actions; these are the first majors on Node 24.
    text = (REPO_DIR / ".github/workflows/python-ci.yml").read_text(encoding="utf-8")
    floor = {"actions/checkout": 5, "actions/setup-python": 6, "actions/upload-artifact": 5}
    used = re.findall(r"uses:\s*(actions/[\w-]+)@v(\d+)", text)
    assert {name for name, _ in used} == set(floor)
    old = [f"{name}@v{major}" for name, major in used if int(major) < floor[name]]
    assert old == []


def test_shared_specs_require_every_sdk_to_test_on_linux_windows_and_macos() -> None:
    spec = (REPO_DIR / "specifications" / "sdk-behaviour.md").read_text(encoding="utf-8")
    rows = [line for line in spec.splitlines() if line.startswith("| Operating systems |")]
    assert len(rows) == 1, "sdk-behaviour.md lacks the Operating systems row"
    for needle in ("Linux", "Windows", "macOS", "USERPROFILE", "relative tolerance", "thread"):
        assert needle in rows[0], needle
    guide = (REPO_DIR / "specifications" / "new-language-sdk.md").read_text(encoding="utf-8")
    assert "Linux, Windows and macOS" in guide and "Operating systems" in guide
    # Owner 2026-11-17: a public repository runs macOS on every push and pull request.
    for text in (rows[0], guide):
        assert "public repository runs macOS on every push and pull request" in text


def test_public_commits_carry_only_the_tradesmart_author() -> None:
    # Owner 2026-11-16: no co-author lines naming tools or assistants in any public repository.
    guide = (REPO_DIR / "specifications" / "new-language-sdk.md").read_text(encoding="utf-8")
    assert "only the TradeSmart author" in guide and "Co-Authored-By" in guide
    internal_rules = REPO_DIR / "CLAUDE.md"  # not exported: checked in the internal repository only
    if internal_rules.exists():
        rules = internal_rules.read_text(encoding="utf-8")
        assert "Co-Authored-By: Claude" in rules and "every GitHub repository" in rules
