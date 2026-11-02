"""SPEC §5.1: TASKQ_LOG_LEVEL / TASKQ_LOG_FORMAT shape the service's logs, and
`python -m taskq_api serve` listens on TASKQ_HOST:TASKQ_PORT.

NFR-04: nothing matching the secret pattern leaves through a log line.
"""
import json
import logging
import sys

import pytest
from sqlalchemy import create_engine

from taskq_api import __main__ as cli
from taskq_api.app import configure_logging, create_app
from taskq_api.models.base import Base

_LOGGERS = ("taskq_api", "uvicorn")


@pytest.fixture(autouse=True)
def _restore_logging():
    """configure_logging is process-wide; leave the other tests' loggers as found."""
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level,
                 logging.getLogger(n).propagate) for n in _LOGGERS}
    yield
    for name, (handlers, level, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _lines(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]


def test_json_format_emits_one_parseable_object_per_line(capsys):
    configure_logging("INFO", "json")
    logging.getLogger("taskq_api.sample").info("hello %s", "world")
    (line,) = _lines(capsys)
    record = json.loads(line)
    assert record["level"] == "INFO"
    assert record["logger"] == "taskq_api.sample"
    assert record["message"] == "hello world"
    assert record["ts"].endswith("+00:00")
    assert "exc" not in record


def test_json_format_carries_the_traceback_in_exc(capsys):
    configure_logging("INFO", "json")
    try:
        1 / 0
    except ZeroDivisionError:
        logging.getLogger("taskq_api.sample").exception("boom")
    record = json.loads(_lines(capsys)[0])
    assert record["message"] == "boom"
    assert "ZeroDivisionError" in record["exc"]


def test_text_format_is_a_plain_line_not_json(capsys):
    configure_logging("INFO", "text")
    logging.getLogger("taskq_api.sample").warning("plain %d", 7)
    (line,) = _lines(capsys)
    assert "WARNING taskq_api.sample plain 7" in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_the_level_filters_what_is_emitted(capsys):
    configure_logging("WARNING", "json")
    log = logging.getLogger("taskq_api.sample")
    log.info("quiet")
    log.warning("loud")
    assert [json.loads(ln)["message"] for ln in _lines(capsys)] == ["loud"]


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_a_secret_in_a_log_line_is_redacted_in_both_formats(capsys, fmt):
    configure_logging("INFO", fmt)
    logging.getLogger("taskq_api.sample").info("key sk-ABCDEFGH12345678 leaked")
    out = capsys.readouterr().err
    assert "sk-ABCDEFGH12345678" not in out
    assert "[REDACTED]" in out


def test_a_secret_in_a_traceback_is_redacted(capsys):
    configure_logging("INFO", "json")
    try:
        raise RuntimeError("bad key sk-ABCDEFGH12345678")
    except RuntimeError:
        logging.getLogger("taskq_api.sample").exception("failed")
    out = capsys.readouterr().err
    assert "sk-ABCDEFGH12345678" not in out and "RuntimeError" in out


def test_configuring_twice_leaves_exactly_one_handler(capsys):
    configure_logging("INFO", "json")
    configure_logging("DEBUG", "text")
    assert len(logging.getLogger("taskq_api").handlers) == 1
    logging.getLogger("taskq_api.sample").debug("once")
    assert len(_lines(capsys)) == 1


def test_application_logs_still_reach_the_root_logger_for_caplog(caplog):
    configure_logging("INFO", "json")
    with caplog.at_level(logging.INFO, logger="taskq_api"):
        logging.getLogger("taskq_api.sample").info("seen by caplog")
    assert "seen by caplog" in caplog.text


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'taskq.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def test_create_app_applies_the_configured_level(env, monkeypatch):
    monkeypatch.setenv("TASKQ_LOG_LEVEL", "debug")
    monkeypatch.setenv("TASKQ_LOG_FORMAT", "text")
    create_app()
    assert logging.getLogger("taskq_api").level == logging.DEBUG


@pytest.mark.parametrize("name,value", [("TASKQ_LOG_LEVEL", "TRACE"), ("TASKQ_LOG_FORMAT", "xml")])
def test_create_app_refuses_an_invalid_logging_value(env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        create_app()


def test_serve_listens_on_the_configured_host_and_port(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("TASKQ_HOST", "127.0.0.2")
    monkeypatch.setenv("TASKQ_PORT", "9123")
    monkeypatch.setenv("TASKQ_LOG_LEVEL", "WARNING")
    monkeypatch.setattr(sys, "argv", ["taskq_api", "serve"])
    cli.main()
    ((args, kwargs),) = calls
    assert args == ("taskq_api.app:create_app",)
    assert kwargs == {"factory": True, "host": "127.0.0.2", "port": 9123, "log_config": None}
    assert logging.getLogger("taskq_api").level == logging.WARNING


def test_serve_defaults_to_the_loopback_address(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    for key in ("TASKQ_HOST", "TASKQ_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["taskq_api", "serve"])
    cli.main()
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 8000


def test_serve_with_an_invalid_port_fails_before_starting_the_server(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("TASKQ_PORT", "99999")
    monkeypatch.setattr(sys, "argv", ["taskq_api", "serve"])
    with pytest.raises(ValueError, match="TASKQ_PORT"):
        cli.main()
    assert calls == []
