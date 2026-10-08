"""Metrics: task counts, execution latency percentiles, rate-limit rejections.

[FR-09] Citations: SPEC.md:158.
"""
import threading
from typing import Any

from taskq_api.repository import tasks as repo
from taskq_api.repository.session import DbSession

PERCENTILES = (50, 90, 99)


class RejectionCounter:
    """Thread-safe count of rate-limit (429) rejections. [FR-09]"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def increment(self) -> None:
        with self._lock:
            self._count += 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


def percentile(sorted_values: list[int], pct: int) -> float:
    """Nearest-rank percentile; 0.0 for no data. [FR-09]"""
    if not sorted_values:
        return 0.0
    rank = max(1, -(-pct * len(sorted_values) // 100))
    return float(sorted_values[rank - 1])


def snapshot(session: DbSession, rejections: RejectionCounter) -> dict[str, Any]:
    """Assemble the /v1/metrics body. [FR-09]"""
    durations = sorted(repo.durations_ms(session))
    return {
        "task_counts": repo.count_by_status(session),
        "latency_percentiles": {f"p{p}": percentile(durations, p) for p in PERCENTILES},
        "rate_limit_rejections": rejections.count,
    }
