import os
import signal
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _generous_rate_limit(monkeypatch):
    """[FR-05] Keep other FRs' tests clear of the rate limiter; test_fr05 overrides these."""
    monkeypatch.setenv("TASKQ_RATE_BURST", "100000")
    monkeypatch.setenv("TASKQ_RATE_PER_SEC", "100000")


def pytest_configure(config):
    """NFR-08: under mutation testing one failing test already kills the mutant, so stop at the first."""
    if os.environ.get("HARNESS_MUTATION_BASELINE") == "1":
        config.option.maxfail = 1


@pytest.fixture(autouse=True)
def _hang_guard():
    """NFR-08: turn a hung test into a failure after 8s so a broken executor cannot stall the suite."""
    def _expired(signum, frame):
        raise TimeoutError("test exceeded 8s")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, 8)
    yield
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, previous)
