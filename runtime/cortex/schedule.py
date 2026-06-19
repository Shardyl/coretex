"""Schedule date math (pure helpers).

Phase 3 retired the old `scheduled_tasks` table — recurring jobs and one-off scheduled work now live in
`tasks` and are driven by `engine.promote_due_tasks()`. What remains here is the cadence arithmetic those
paths reuse: `next_run()` (daily/weekly/monthly) and `first_of_next_month()` (newsletter monthly stacking).
"""
from __future__ import annotations

import calendar
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


def _on_day(after: datetime, year: int, month: int, day: int, hour: int) -> datetime:
    d = min(day, calendar.monthrange(year, month)[1])   # clamp (e.g. day 31 in a short month)
    return after.replace(year=year, month=month, day=d, hour=hour, minute=0, second=0, microsecond=0)


def day_of_month(after: datetime, day: int, hour: int = 9) -> datetime:
    """The `day`-of-month slot at or after `after`: this month's `day` if still ahead, else next month's
    (used to schedule a company's content on its fixed monthly publishing day)."""
    this = _on_day(after, after.year, after.month, day, hour)
    if this > after:
        return this
    y, m = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
    return _on_day(after, y, m, day, hour)


def next_month_day(after: datetime, day: int, hour: int = 9) -> datetime:
    """The `day`-of-month in the month AFTER `after`'s month (for stacking one item per month)."""
    y, m = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
    return _on_day(after, y, m, day, hour)
