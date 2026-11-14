from pathlib import Path

import pytest

LIVE_DIR = Path(__file__).resolve().parents[1] / "live"
LIVE_MODULES = sorted(LIVE_DIR.glob("*.py"))
FORBIDDEN = (
    ".place_order(",
    ".modify_order(",
    ".cancel_order(",
    ".exit_order(",
    ".request(",
    ".post(",
    ".put(",
    ".delete(",
    ".execute(",
    ".generate_session(",
    ".set_access_token(",
    "GenAcsTok",
    ".order_margin(",
    "state_changing=True",
    "PlaceOrder",
    "ModifyOrder",
    "CancelOrder",
    "ExitSNOOrder",
)
# The one exception: the UAT matrix family M10 reads GetOrderMargin (a documented read, not
# state-changing in the SDK: nothing is placed) from this one module, UAT mode only. Every other
# forbidden call stays forbidden there too.
ALLOWED = frozenset({("test_live_uat_margin.py", ".order_margin(")})


def test_live_modules_are_found() -> None:
    names = {p.name for p in LIVE_MODULES}
    assert {"test_live_readonly.py", "conftest.py", "_helpers.py"} <= names


@pytest.mark.parametrize("call", FORBIDDEN)
@pytest.mark.parametrize("module", LIVE_MODULES, ids=lambda p: p.name)
def test_live_suite_makes_no_state_changing_calls(module: Path, call: str) -> None:
    if (module.name, call) in ALLOWED:
        return
    assert call not in module.read_text(encoding="utf-8")


def test_the_only_exception_is_the_order_margin_read_of_the_margin_family() -> None:
    assert {("test_live_uat_margin.py", ".order_margin(")} == ALLOWED
    module = LIVE_DIR / "test_live_uat_margin.py"
    text = module.read_text(encoding="utf-8")
    assert text.count(".order_margin(") == 1  # one read, in one place
    assert "uat_mode(" in text  # UAT mode only
