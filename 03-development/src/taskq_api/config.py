"""Environment-backed settings (leaf module; imports nothing from the package). [FR-05]"""
import os


def rate_burst() -> int:
    """TASKQ_RATE_BURST: token-bucket capacity (default 20)."""
    return int(os.environ.get("TASKQ_RATE_BURST", "20"))


def rate_per_sec() -> float:
    """TASKQ_RATE_PER_SEC: token refill rate per second (default 5.0)."""
    return float(os.environ.get("TASKQ_RATE_PER_SEC", "5.0"))
