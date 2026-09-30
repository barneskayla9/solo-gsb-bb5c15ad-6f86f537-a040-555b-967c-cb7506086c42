"""Appointment domain logic.

Glue between the published calendar (:mod:`app.scheduler`) and the
appointment storage: it answers "is this UTC range fully contained in one
continuous open interval of the current calendar?"

The calendar engine clips every session to local days, so an overnight
session surfaces as two touching pieces (one on each side of local midnight,
DST seam included).  Those pieces are re-merged here before containment is
tested, which is exactly why an appointment is allowed to cross local
midnight.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from .scheduler import RuleSet, build_calendar, get_zone


def merged_open_intervals(
    tz_name: str,
    rules: RuleSet,
    utc_from: datetime,
    utc_to: datetime,
    *,
    pad_days: int = 1,
) -> list[tuple[datetime, datetime]]:
    """Ascending, disjoint, maximal open UTC intervals covering an UTC range.

    The expansion window is computed from the *local* dates of the bounds: an
    evening session east of UTC (e.g. Friday 23:00 in Shanghai) is still
    Thursday in UTC and would otherwise be missed.  Weekly sessions spill at
    most one local day, so one day of padding suffices; the extra margin also
    absorbs DST shifts.

    Intervals that overlap or merely touch — including two halves of an
    overnight session split at local midnight — are one continuous interval.
    """
    zone = get_zone(tz_name)
    date_from = utc_from.astimezone(zone).date() - timedelta(days=pad_days)
    date_to = utc_to.astimezone(zone).date() + timedelta(days=pad_days)
    days = build_calendar(tz_name, rules, date_from, date_to)
    pieces = sorted(
        (start, end)
        for day in days
        for start, end, _sources in day.intervals
    )
    merged: list[list[datetime]] = []
    for start, end in pieces:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def is_fully_open(
    intervals: Iterable[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
) -> bool:
    """True if a single continuous open interval contains the whole request.

    Touching is containment: an appointment ending exactly when an opening
    ends is valid, as is one starting exactly when it opens.
    """
    return any(open_start <= start and open_end >= end for open_start, open_end in intervals)


def conflicting_future_appointments(
    tz_name: str,
    rules: RuleSet,
    rows: Iterable[dict],
) -> list[int]:
    """IDs of appointments not fully open under ``rules`` in ``tz_name``.

    ``rows`` is an iterable of ``{"id", "start_at", "end_at"}`` already
    filtered to active/future appointments.
    """
    rows = list(rows)
    if not rows:
        return []
    intervals = merged_open_intervals(
        tz_name,
        rules,
        min(row["start_at"] for row in rows),
        max(row["end_at"] for row in rows),
    )
    return [
        row["id"]
        for row in rows
        if not is_fully_open(intervals, row["start_at"], row["end_at"])
    ]
