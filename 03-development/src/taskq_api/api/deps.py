"""Route dependencies.

[FR-03/FR-04] Citations: SPEC.md:101-113.
"""
from taskq_api.service.auth import require_scope

__all__ = ["require_scope"]
