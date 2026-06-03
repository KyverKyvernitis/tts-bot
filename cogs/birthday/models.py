from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class CalendarEntry:
    user_id: int
    day: int
    month: int
    year: int | None
    display_name: str
    mention: str
    next_dt: datetime
