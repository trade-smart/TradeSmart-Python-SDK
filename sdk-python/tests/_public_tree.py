"""What counts as the public tree, and which strings must never appear in it.

Shared by the layout guard tests (``tests/unit/test_repo_layout.py``) and used by the export
script that builds the public repository, so both apply the same rules. The rules are pure
functions of repo-relative POSIX paths and of text (``is_public``, ``is_exempt``, ``scan``): the
export applies them to a file list and to a staging copy without touching the live tree.
``public_files`` is only a thin wrapper that walks a tree and applies ``is_public``.

Public tree: every file except the internal-only paths below. Public text files must not mention
``FORBIDDEN_PATTERNS``, except the files named by ``EXEMPT_FROM_REFERENCE_CHECK`` (an explicit
list). No public file may cite an internal review-ruling id (``PROCESS_ID_RE``).
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

REPO_DIR = Path(__file__).resolve().parents[2]

# Internal-only at the repository root only (a public folder of that name deeper down is fine).
ROOT_ONLY_SKIPPED = frozenset({"internal", ".claude", ".superpowers", ".worktrees"})

# Never public at any depth: VCS data, tool caches, build output, local editor/env folders, and
# the observations of the live suites (``sdk-python/.live-results``; they come from the test
# account). ``.git`` is a file, not a folder, in a git worktree, so the check applies to files too.
SKIPPED_ANYWHERE = frozenset(
    {
        ".git",
        ".live-results",
        ".venv",
        ".tox",
        ".idea",
        ".vscode",
        "node_modules",
        "dist",
        "build",
        "htmlcov",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
    }
)

# File names (any depth, any case) that exist in the internal checkout only: agent instructions.
INTERNAL_ONLY_FILES = frozenset({"CLAUDE.md", "CLAUDE.local.md"})

# Untracked local files (by file name, fnmatch) that are never public.
LOCAL_FILE_PATTERNS = (".env", ".env.*", ".coverage", ".coverage.*")

# Strings that name internal material (case-insensitive). The bare word "internal" in prose is
# fine: only a path (``internal/``, or ``internal`` closing a link, code span or quote) is not.
# Open-item ids ("F19") are checked separately: they are defined in the public behaviour
# contract, see ``OPEN_ITEM_TABLE``.
FORBIDDEN_PATTERNS = (
    r"internal/",
    r"(?<![\w-])internal(?=[)\]`\"'>])",
    r"plot\.md",
    r"Master_Prompt",
    r"CLAUDE\.md",
    r"\.claude/",
    r"\.superpowers",
    r"docs/superpowers",
)
FORBIDDEN_RE = re.compile("|".join(FORBIDDEN_PATTERNS), re.IGNORECASE)

# Ids of internal review rulings (a letter and a number after "ruling"): they mean nothing in the
# public repository. Checked in every public file, the exempt ones included.
PROCESS_ID_RE = re.compile(r"\bruling\s+[A-Z]\d+\b", re.IGNORECASE)

# Tests about internal material are internal-only: they run in the internal checkout and are left
# out of the export.
# Matched case-insensitively (lower-case globs) in every SDK folder; fnmatch's ``*`` also
# matches ``/``, so the second glob covers every depth below ``tests/``.
INTERNAL_ONLY_TESTS = (
    "sdk-*/tests/test_internal_*.py",
    "sdk-*/tests/**/test_internal_*.py",
)

# Public files allowed to name the patterns, by repo-relative posix glob (fnmatch): ignore rules
# are not references; internal-only tests are not exported; this module and the layout guard
# hold the patterns (and their negative controls).
EXEMPT_FROM_REFERENCE_CHECK = (
    ".gitignore",
    *INTERNAL_ONLY_TESTS,
    "sdk-python/tests/_public_tree.py",
    "sdk-python/tests/unit/test_repo_layout.py",
)

OPEN_ITEM_TABLE = "specifications/sdk-behaviour.md"
OPEN_ITEM_RE = re.compile(r"\bF\d{1,3}\b")
# File types scanned for open-item ids (``F19``), by suffix, plus ``Dockerfile``.
OPEN_ITEM_FILES = frozenset(
    {".md", ".py", ".toml", ".yml", ".yaml", ".txt", ".sh", ".json", ".ps1"}
)
_OPEN_ITEM_ROW = re.compile(r"^\|\s*(F\d{1,3})\s*\|", re.M)

# Other brokers may be named only in the migration guides (spelled in pieces so that this file
# does not name them itself).
MIGRATION_GUIDES = "sdk-python/docs/migration/"
BROKER_RE = re.compile(
    r"\b("
    + "|".join(["zero" + "dha", "ki" + "te", "dh" + "an", "dh" + "anhq", "up" + "stox"])
    + r")\b",
    re.IGNORECASE,
)


_DRIVE = re.compile(r"^[A-Za-z]:")
_ROOT_ONLY_CF = frozenset(name.casefold() for name in ROOT_ONLY_SKIPPED)
_ANYWHERE_CF = frozenset(name.casefold() for name in SKIPPED_ANYWHERE)
_INTERNAL_FILES_CF = frozenset(name.casefold() for name in INTERNAL_ONLY_FILES)


def normalise(rel: str) -> str | None:
    """The repo-relative POSIX form of a listed path, or ``None`` when it is not one.

    Backslashes become ``/`` and leading ``./`` is dropped; absolute and drive paths, ``..``,
    inner ``.`` and empty segments (``a//b``, a trailing ``/``) are rejected.
    """
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.startswith("/") or _DRIVE.match(rel):
        return None
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return None
    return rel


def is_public(rel: str) -> bool:
    """Whether the repo-relative path (file or folder) belongs in the public tree.

    The path is normalised first and classified case-insensitively, so ``INTERNAL/x`` and
    ``claude.md`` (the same files on a case-insensitive disk) are internal too.
    """
    norm = normalise(rel[:-1] if rel.endswith("/") else rel)
    if norm is None:
        return False
    parts = norm.casefold().split("/")
    if parts[0] in _ROOT_ONLY_CF or parts[-1] in _INTERNAL_FILES_CF:
        return False
    if any(part in _ANYWHERE_CF or part.endswith(".egg-info") for part in parts):
        return False
    return not any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in LOCAL_FILE_PATTERNS)


def is_internal_only_test(rel: str) -> bool:
    """Whether the path is an internal-only test (``sdk-*/tests/**/test_internal_*.py``)."""
    norm = normalise(rel)
    folded = (norm if norm is not None else rel.replace("\\", "/")).casefold()
    return any(fnmatch.fnmatchcase(folded, pattern) for pattern in INTERNAL_ONLY_TESTS)


def is_exported(rel: str) -> bool:
    """Whether the listed file goes into the public export: a well-formed path, public, and not
    an internal-only test."""
    return is_public(rel) and not is_internal_only_test(rel)


def is_exempt(rel: str) -> bool:
    """Whether the public file may name the forbidden patterns."""
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in EXEMPT_FROM_REFERENCE_CHECK)


def internal_references(text: str) -> list[str]:
    """Forbidden strings found in ``text`` (empty when clean)."""
    return sorted({m.group(0) for m in FORBIDDEN_RE.finditer(text)})


def scan(rel: str, text: str) -> list[str]:
    """Forbidden strings in the public file ``rel`` with content ``text`` (exempt files: none)."""
    return [] if is_exempt(rel) else internal_references(text)


def public_files(root: Path = REPO_DIR) -> Iterator[Path]:
    """Every public file under ``root`` (a working tree), sorted."""
    for folder, dirs, files in os.walk(root):
        base = Path(folder).relative_to(root).as_posix()
        prefix = "" if base == "." else base + "/"
        dirs[:] = sorted(d for d in dirs if is_public(prefix + d))
        for name in sorted(files):
            if is_public(prefix + name):
                yield Path(folder) / name


def read_text(path: Path) -> str | None:
    """File text, or ``None`` for a binary or unreadable (for example vanished) file."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def names_internal_material(line: str) -> bool:
    """Stricter check for shipped code (``src/``): even the bare word "superpowers" is rejected."""
    return bool(internal_references(line)) or "superpowers" in line.lower()


_NOQA_COMMENT = re.compile(r"#\s*noqa\b[^\n]*", re.IGNORECASE)


def open_item_ids(text: str) -> set[str]:
    """Open-item ids (``F19``) named in ``text``; lint codes in ``# noqa`` comments are not ids."""
    return set(OPEN_ITEM_RE.findall(_NOQA_COMMENT.sub("", text)))


def defined_open_items(table_text: str) -> set[str]:
    """Open-item ids defined by a row of the contract's table (``| F19 | ...``)."""
    return set(_OPEN_ITEM_ROW.findall(table_text))


def undefined_open_items(rel: str, text: str, defined: set[str]) -> list[str]:
    """``"<rel>: F99"`` for each open-item id in a scanned file type that ``defined`` lacks."""
    path = PurePosixPath(rel)
    if path.suffix not in OPEN_ITEM_FILES and path.name != "Dockerfile":
        return []
    return [f"{rel}: {item}" for item in sorted(open_item_ids(text) - defined)]


def other_broker_names(rel: str, text: str) -> list[str]:
    """Other brokers named in ``text`` (none allowed outside the migration guides)."""
    if rel.startswith(MIGRATION_GUIDES):
        return []
    return sorted({m.group(0) for m in BROKER_RE.finditer(text)})


def process_ids(text: str) -> list[str]:
    """Internal review-ruling ids cited in ``text`` (empty when clean)."""
    return sorted({m.group(0) for m in PROCESS_ID_RE.finditer(text)})
