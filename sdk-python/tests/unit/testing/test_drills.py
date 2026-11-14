import dataclasses

import pytest

from tradesmart.testing import Drill, drills


def test_each_helper_builds_its_kind() -> None:
    assert drills.reject("RMS: margin") == Drill("reject", message="RMS: margin")
    assert drills.timeout_after_send() == Drill("timeout_after_send")
    assert drills.connect_error() == Drill("connect_error")
    assert drills.rate_limit() == Drill("rate_limit")
    assert drills.rate_limit(retry_after=2.5) == Drill("rate_limit", retry_after=2.5)
    assert drills.server_error() == Drill("server_error", status=503)
    assert drills.server_error(status=502) == Drill("server_error", status=502)
    assert drills.server_error(json=True) == Drill("server_error", status=503, json=True)
    assert drills.server_error().json is False  # a gateway page by default, like a real 5xx
    assert drills.bad_body() == Drill("bad_body")
    assert drills.session_expired() == Drill("session_expired")


def test_drill_is_frozen() -> None:
    drill = drills.bad_body()
    with pytest.raises(dataclasses.FrozenInstanceError):
        drill.kind = "reject"  # type: ignore[misc]


def test_drills_module_is_exported_from_package() -> None:
    import tradesmart.testing as testing

    assert testing.drills is drills
    assert {"Drill", "OfflineOrder", "OfflineRequest", "drills"} <= set(testing.__all__)
