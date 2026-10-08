"""Environment-backed settings (leaf module; imports nothing from the package). [FR-05]"""
import os


def rate_burst() -> int:
    """TASKQ_RATE_BURST: token-bucket capacity (default 20)."""
    return int(os.environ.get("TASKQ_RATE_BURST", "20"))


def rate_per_sec() -> float:
    """TASKQ_RATE_PER_SEC: token refill rate per second (default 5.0)."""
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
