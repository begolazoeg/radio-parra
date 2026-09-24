"""
Parrilla de Radio Parra (§4.3): scheduler puro, reglas de franjas/interrupciones y
presupuesto de charla. La configuración (``GridConfig``) vive en ``radio.core.config``.
"""

from radio.grid.rules import TIME_SIGNAL_WINDOW_MIN, next_interrupt_at, parse_when
from radio.grid.scheduler import (
    PlayUnit,
    SchedulerState,
    advance_state,
    history_horizon,
    next_unit,
)

__all__ = [
    "TIME_SIGNAL_WINDOW_MIN",
    "PlayUnit",
    "SchedulerState",
    "advance_state",
    "history_horizon",
    "next_interrupt_at",
    "next_unit",
    "parse_when",
]
