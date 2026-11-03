import json
import logging
import sys
from types import SimpleNamespace

import pytest

from app.core import logging as app_logging


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers, root.level = handlers, level


def record(msg="hello %s", args=("world",), exc_info=None):
    return logging.LogRecord("fuelguard.test", logging.WARNING, __file__, 1, msg, args, exc_info)


def test_json_formatter_outputs_one_json_object():
    out = json.loads(app_logging.JsonFormatter().format(record()))
    assert out["message"] == "hello world" and out["level"] == "WARNING"
    assert out["logger"] == "fuelguard.test" and "time" in out and "exception" not in out


def test_json_formatter_includes_the_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        out = json.loads(app_logging.JsonFormatter().format(record(exc_info=sys.exc_info())))
    assert "ValueError: boom" in out["exception"]


@pytest.mark.parametrize("fmt, formatter", [("json", app_logging.JsonFormatter), ("text", logging.Formatter)])
def test_setup_logging_follows_the_configured_format(monkeypatch, restore_root_logger, fmt, formatter):
    monkeypatch.setattr(app_logging, "get_settings", lambda: SimpleNamespace(log_format=fmt, log_level="debug"))
    app_logging.setup_logging()
    (handler,) = restore_root_logger.handlers
    assert type(handler.formatter) is formatter
    assert restore_root_logger.level == logging.DEBUG
