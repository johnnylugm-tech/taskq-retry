"""Secret redaction for run output tails: a line holding a secret is replaced whole.

[NFR-04] Sensitive-line redaction.

[FR-02] Citations: SPEC.md:209-210 (NFR-04 pattern redaction; module per SAD.md service.redaction).
"""
import re

_PATTERN = r"(?:sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+|postgres(?:ql)?://\S+)"
_SECRET = re.compile(_PATTERN)
_SECRET_LINE = re.compile(rf"^[^\n]*{_PATTERN}[^\n]*$", re.MULTILINE)


def redact(text: str) -> str:
    """Replace every line that holds a secret with [REDACTED]. [NFR-04]"""
    return _SECRET_LINE.sub("[REDACTED]", text)


def redact_inline(text: str) -> str:
    """Replace only the secret substrings, keeping surrounding text (log records keep their structure). [NFR-04]"""
    return _SECRET.sub("[REDACTED]", text)
