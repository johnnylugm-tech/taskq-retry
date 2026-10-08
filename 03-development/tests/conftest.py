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
