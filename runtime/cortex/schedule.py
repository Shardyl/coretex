"""Schedule date math (pure helpers).

Phase 3 retired the old `scheduled_tasks` table — recurring jobs and one-off scheduled work now live in
`tasks` and are driven by `engine.promote_due_tasks()`. What remains here is the cadence arithmetic those
paths reuse: `next_run()` (daily/weekly/monthly) and `first_of_next_month()` (newsletter monthly stacking).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_GST = timezone(timedelta(hours=4))   # Cortex standard time: GST (GMT+4, no DST) — schedules fire on GST clock


def next_run(cadence: str, weekday: int, hour: int, minute: int, after=None) -> datetime:
    now = after or datetime.now(_GST)
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cadence == "daily":
        return t if t > now else t + timedelta(days=1)
    if cadence == "monthly":
        return t + timedelta(days=30) if t <= now else t
    days = (weekday - t.weekday()) % 7        # weekly (default)
    t += timedelta(days=days)
    return t if t > now else t + timedelta(days=7)


def first_of_next_month(after: datetime, hour: int = 9) -> datetime:
    y, m = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
    return after.replace(year=y, month=m, day=1, hour=hour, minute=0, second=0, microsecond=0)
