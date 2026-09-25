"""Lectura tolerante de JSON de APIs externas: un tipo inesperado da un valor vacío."""

from __future__ import annotations

from typing import Any


def get_str(data: Any, key: str) -> str:
    """``data[key]`` si es un str (sin espacios extremos); si no, ``""``."""
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def get_dict(data: Any, key: str) -> dict[str, Any]:
    """``data[key]`` si es un dict; si no, ``{}``."""
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def get_list(data: Any, key: str) -> list[Any]:
    """``data[key]`` si es una lista; si no, ``[]``."""
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []
