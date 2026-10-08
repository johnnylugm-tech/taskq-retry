"""Rate-limit hook wiring: environment config and app installation.

[FR-05] Citations: SPEC.md:115-120. Enforcement happens in the shared
`require_scope` dependency (SAD section 4); /healthz and /readyz never use it.
"""
from typing import NamedTuple

from fastapi import FastAPI

from taskq_api import config as config_module


class RateConfig(NamedTuple):
    """Bucket capacity and refill rate. [FR-05]"""

    burst: int
    per_sec: float


def load_rate_config() -> RateConfig:
    """Read TASKQ_RATE_BURST (default 20) and TASKQ_RATE_PER_SEC (default 5.0); both must be positive. [FR-05]"""
    config = RateConfig(config_module.rate_burst(), config_module.rate_per_sec())
    if config.burst < 1 or config.per_sec <= 0:
        raise ValueError("TASKQ_RATE_BURST must be >= 1 and TASKQ_RATE_PER_SEC > 0")
    return config


def install_rate_limit(app: FastAPI) -> None:
    """Store the rate config where the auth dependency reads it. [FR-05]"""
    app.state.rate_config = load_rate_config()
