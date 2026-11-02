"""SPEC §5.1: the five environment keys that configure the delivered service.

TASKQ_CORS_ORIGINS, TASKQ_LOG_LEVEL, TASKQ_LOG_FORMAT, TASKQ_HOST and
TASKQ_PORT are read by `taskq_api.config`. Defaults are the SPEC table's; a
value outside the documented set fails fast and names the variable.
"""
import pytest

from taskq_api import config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("TASKQ_CORS_ORIGINS", "TASKQ_LOG_LEVEL", "TASKQ_LOG_FORMAT",
                "TASKQ_HOST", "TASKQ_PORT"):
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_the_spec_table(monkeypatch):
    assert config.cors_origins() == []
    assert config.log_level() == "INFO"
    assert config.log_format() == "json"
    assert config.host() == "127.0.0.1"
    assert config.port() == 8000


def test_an_empty_cors_value_is_deny_all(monkeypatch):
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", "")
    assert config.cors_origins() == []


def test_cors_origins_are_split_trimmed_and_blank_entries_dropped(monkeypatch):
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", " https://a.example ,https://b.example,, ")
    assert config.cors_origins() == ["https://a.example", "https://b.example"]


def test_a_wildcard_origin_is_refused(monkeypatch):
    monkeypatch.setenv("TASKQ_CORS_ORIGINS", "https://a.example,*")
    with pytest.raises(ValueError, match="TASKQ_CORS_ORIGINS"):
        config.cors_origins()


@pytest.mark.parametrize("raw,expected", [("debug", "DEBUG"), ("Warning", "WARNING"), ("ERROR", "ERROR")])
def test_log_level_accepts_the_documented_set_case_insensitively(monkeypatch, raw, expected):
    monkeypatch.setenv("TASKQ_LOG_LEVEL", raw)
    assert config.log_level() == expected


@pytest.mark.parametrize("raw", ["TRACE", "CRITICAL", ""])
def test_log_level_outside_the_documented_set_names_the_variable(monkeypatch, raw):
    monkeypatch.setenv("TASKQ_LOG_LEVEL", raw)
    with pytest.raises(ValueError, match="TASKQ_LOG_LEVEL"):
        config.log_level()


@pytest.mark.parametrize("raw,expected", [("TEXT", "text"), ("json", "json")])
def test_log_format_accepts_the_documented_set_case_insensitively(monkeypatch, raw, expected):
    monkeypatch.setenv("TASKQ_LOG_FORMAT", raw)
    assert config.log_format() == expected


@pytest.mark.parametrize("raw", ["xml", ""])
def test_log_format_outside_the_documented_set_names_the_variable(monkeypatch, raw):
    monkeypatch.setenv("TASKQ_LOG_FORMAT", raw)
    with pytest.raises(ValueError, match="TASKQ_LOG_FORMAT"):
        config.log_format()


def test_host_is_taken_from_the_environment(monkeypatch):
    monkeypatch.setenv("TASKQ_HOST", "0.0.0.0")  # nosec B104 - test value, not a bind
    assert config.host() == "0.0.0.0"  # nosec B104


def test_an_empty_host_falls_back_to_the_loopback_default(monkeypatch):
    """An empty address would bind every interface in some servers; the SPEC default is not exposed."""
    monkeypatch.setenv("TASKQ_HOST", "")
    assert config.host() == "127.0.0.1"


def test_port_is_taken_from_the_environment(monkeypatch):
    monkeypatch.setenv("TASKQ_PORT", "9123")
    assert config.port() == 9123


@pytest.mark.parametrize("raw", ["abc", "", "0", "65536", "-1", "80.5"])
def test_port_outside_1_to_65535_or_not_an_integer_names_the_variable(monkeypatch, raw):
    monkeypatch.setenv("TASKQ_PORT", raw)
    with pytest.raises(ValueError, match="TASKQ_PORT"):
        config.port()
