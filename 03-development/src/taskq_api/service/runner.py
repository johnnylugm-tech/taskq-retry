"""Run state machine.

[FR-02] Citations: SPEC.md:97.
"""
INITIAL_STATUS = "pending"

_TRANSITIONS = {
    "pending": {"running"},
    "running": {"done", "failed", "timeout"},
    "done": set(),
    "failed": set(),
    "timeout": set(),
}


def is_transition_allowed(from_status: str, to_status: str) -> bool:
    """Return True if `from_status -> to_status` is a legal run transition."""
    return to_status in _TRANSITIONS.get(from_status, set())
