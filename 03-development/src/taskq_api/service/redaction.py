"""Secret redaction for run output tails.

[FR-02] Citations: SPEC.md:209-210 (NFR-04 pattern redaction; module per SAD.md service.redaction).
"""
import re

_SECRET = re.compile(r"sk-[A-Za-z0-9]{8,}")


def redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text)
