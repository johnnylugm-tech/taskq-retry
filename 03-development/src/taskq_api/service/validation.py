"""Task input validation rules.

[FR-01] Citations: SPEC.md:88 (non-empty / <=1000 chars / injection blacklist / unique name).
The blacklist is undefined in SPEC.md (NFR-99 item 1); NUL and line breaks are rejected.
"""
MAX_COMMAND_LEN = 1000
FORBIDDEN_COMMAND_CHARS = frozenset("\x00\r\n")


def validate_command(value: str) -> str:
    """Reject commands containing blacklisted characters."""
    if FORBIDDEN_COMMAND_CHARS & set(value):
        raise ValueError("command contains forbidden characters")
    return value
