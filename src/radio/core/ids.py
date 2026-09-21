"""
Generación de identificadores ULID únicos y ordenables.
"""

from __future__ import annotations

from ulid import ULID


def new_id() -> str:
    """Genera un nuevo ULID como string en mayúsculas."""
    return str(ULID())
