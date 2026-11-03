"""Environment-backed settings (leaf module; imports nothing from the package). [FR-05]"""
import os


def rate_burst() -> int:
    """TASKQ_RATE_BURST: token-bucket capacity (default 20). [FR-05]"""
    return int(os.environ.get("TASKQ_RATE_BURST", "20"))


def rate_per_sec() -> float:
    """TASKQ_RATE_PER_SEC: token refill rate per second (default 5.0). [FR-05]"""
    return float(os.environ.get("TASKQ_RATE_PER_SEC", "5.0"))


def db_pool_size() -> int:
    """TASKQ_DB_POOL_SIZE: SQLAlchemy connection pool size (default 5). [FR-06]"""
    return int(os.environ.get("TASKQ_DB_POOL_SIZE", "5"))


def drain_timeout() -> float:
    """TASKQ_DRAIN_TIMEOUT: seconds to wait for in-flight runs on shutdown (default 30.0). [FR-08]"""
    return float(os.environ.get("TASKQ_DRAIN_TIMEOUT", "30.0"))


def max_concurrent() -> int:
    """TASKQ_MAX_CONCURRENT: concurrent run cap (default 4). [FR-08]"""
    return int(os.environ.get("TASKQ_MAX_CONCURRENT", "4"))


def task_timeout() -> float:
    """TASKQ_TASK_TIMEOUT: per-run timeout in seconds (default 300.0). [FR-08]"""
    return float(os.environ.get("TASKQ_TASK_TIMEOUT", "300.0"))


_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_LOG_FORMATS = ("json", "text")


def cors_origins() -> list[str]:
    """TASKQ_CORS_ORIGINS: comma-separated allowed origins; empty (default) allows none. [NFR-02]

    Citations: SPEC.md:193, SPEC.md:298. A wildcard is refused: the SPEC's allowlist is explicit.
    """
    raw = os.environ.get("TASKQ_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if "*" in origins:
        raise ValueError("TASKQ_CORS_ORIGINS must list explicit origins, not '*'")
    return origins


def log_level() -> str:
    """TASKQ_LOG_LEVEL: DEBUG, INFO, WARNING or ERROR (default INFO). [NFR-04]

    Citations: SPEC.md:299.
    """
    value = os.environ.get("TASKQ_LOG_LEVEL", "INFO").upper()
    if value not in _LOG_LEVELS:
        raise ValueError(f"TASKQ_LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
    return value


def log_format() -> str:
    """TASKQ_LOG_FORMAT: json or text (default json). [NFR-04]

    Citations: SPEC.md:300.
    """
    value = os.environ.get("TASKQ_LOG_FORMAT", "json").lower()
    if value not in _LOG_FORMATS:
        raise ValueError(f"TASKQ_LOG_FORMAT must be one of {', '.join(_LOG_FORMATS)}")
    return value


def host() -> str:
    """TASKQ_HOST: listen address (default 127.0.0.1, not exposed); empty means the default. [NFR-02]

    Citations: SPEC.md:301.
    """
    return os.environ.get("TASKQ_HOST") or "127.0.0.1"


def port() -> int:
    """TASKQ_PORT: listen port, 1-65535 (default 8000). [NFR-02]

    Citations: SPEC.md:302.
    """
    raw = os.environ.get("TASKQ_PORT", "8000")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"TASKQ_PORT must be an integer, got {raw!r}") from None
    if not 1 <= value <= 65535:
        raise ValueError(f"TASKQ_PORT must be between 1 and 65535, got {value}")
    return value
