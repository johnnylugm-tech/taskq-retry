"""Declarative base.

[FR-01] Citations: SPEC.md:304-312.
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
