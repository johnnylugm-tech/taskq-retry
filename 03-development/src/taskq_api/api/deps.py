"""Route dependencies.

[FR-03] Citations: SPEC.md:101-103.
"""
from taskq_api.service.auth import require_scope

__all__ = ["require_scope"]
