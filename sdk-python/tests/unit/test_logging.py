import logging

from tradesmart._logging import LOGGER_NAME, get_logger, redact


def test_redact_nested():
    out = redact(
        {"uid": "TEST123", "access_token": "abc", "nested": [{"Authorization": "Bearer abc"}]}
    )
    assert out == {"uid": "TEST123", "access_token": "***", "nested": [{"Authorization": "***"}]}


def test_redact_does_not_mutate():
    src = {"access_token": "abc"}
    redact(src)
    assert src == {"access_token": "abc"}


def test_redact_strings():
    assert redact("Authorization: Bearer abc.def") == "Authorization: Bearer ***"
    assert (
        redact('jData={"code":"c1","checksum":"ff00"}') == 'jData={"code":"***","checksum":"***"}'
    )
    assert redact("a=1&code=xyz&b=2") == "a=1&code=***&b=2"


def test_logger_never_emits_token(caplog):
    caplog.set_level("DEBUG", logger="tradesmart")
    get_logger("t").debug("payload %s", {"susertoken": "secret-value"})
    assert "secret-value" not in caplog.text
    assert "***" in caplog.text


def test_get_logger_names_and_single_handler_filter():
    assert get_logger().name == LOGGER_NAME
    assert get_logger("x").name == "tradesmart.x"
    get_logger("y")
    root = logging.getLogger(LOGGER_NAME)
    assert sum(isinstance(h, logging.NullHandler) for h in root.handlers) == 1
    assert len(root.filters) == 1
