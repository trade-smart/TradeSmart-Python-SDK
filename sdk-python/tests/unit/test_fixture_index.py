"""``specifications/test-fixtures/README.md`` lists every fixture file exactly once."""

import re

from tests.conftest import FIXTURE_DIR


def test_readme_lists_every_fixture_file_exactly_once() -> None:
    text = (FIXTURE_DIR / "README.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `([A-Za-z0-9_]+\.json)` \|", text, flags=re.MULTILINE)
    present = sorted(p.name for p in FIXTURE_DIR.glob("*.json"))
    assert len(rows) == len(set(rows)), "a fixture is listed twice"
    assert sorted(rows) == present
