"""Request/response schemas and rule validation."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from pydantic import BaseModel, Field, field_serializer, field_validator

from .errors import InvalidPeriodError, UnknownTimezoneError
from .scheduler import get_zone

# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _parse_hhmm(value: str, field_name: str) -> time:
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise InvalidPeriodError(
        f"'{field_name}' must be a wall-clock time in HH:MM format, got {value!r}"
    )


class WeeklyRuleIn(BaseModel):
    weekday: int
    start_time: str
    end_time: str
    label: str | None = None


class MakeupRuleIn(BaseModel):
    day: date
    start_time: str
    end_time: str
    label: str | None = None


class RulesPayloadIn(BaseModel):
    weekly: list[WeeklyRuleIn] = Field(default_factory=list)
    closed_dates: list[date] = Field(default_factory=list)
    makeup: list[MakeupRuleIn] = Field(default_factory=list)


class PublishRulesIn(BaseModel):
    expected_version: int = Field(ge=0)
    rules: RulesPayloadIn


class PointCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str


class PointUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None


class AppointmentCreateIn(BaseModel):
    """Book a visit. Both instants must be explicit, timezone-aware UTC times."""

    start_at: datetime
    end_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("start_at", "end_at")
    @classmethod
    def _must_be_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            # A bare timestamp is ambiguous about the instant; refuse it with
            # the normal request-shape error instead of guessing.
            raise ValueError("timestamps must carry an explicit UTC offset, e.g. 'Z'")
        return value.astimezone(timezone.utc)


class AppointmentCancelIn(BaseModel):
    cancel_token: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_timezone(tz_name: str) -> str:
    try:
        get_zone(tz_name)
    except Exception:
        raise UnknownTimezoneError(f"unknown IANA timezone: {tz_name!r}")
    return tz_name


def validate_rules_payload(payload: RulesPayloadIn) -> dict:
    """Validate semantics and return a normalized JSON-safe payload.

    Raises :class:`InvalidPeriodError` for every invalid-period case.
    """
    weekly_out = []
    seen_weekly: set[tuple] = set()
    for rule in payload.weekly:
        if not 1 <= rule.weekday <= 7:
            raise InvalidPeriodError(
                f"weekly weekday must be between 1 (Monday) and 7 (Sunday), "
                f"got {rule.weekday}"
            )
        start = _parse_hhmm(rule.start_time, "start_time")
        end = _parse_hhmm(rule.end_time, "end_time")
        if start == end:
            raise InvalidPeriodError(
                "weekly start_time and end_time must differ; a regular rule is "
                "either same-day (end > start) or overnight (end < start)"
            )
        key = (rule.weekday, start.isoformat(), end.isoformat())
        if key in seen_weekly:
            raise InvalidPeriodError(
                f"duplicate weekly rule: weekday={rule.weekday}, "
                f"{start.isoformat()}-{end.isoformat()}"
            )
        seen_weekly.add(key)
        weekly_out.append(
            {
                "weekday": rule.weekday,
                "start_time": start.strftime("%H:%M"),
                "end_time": end.strftime("%H:%M"),
                **({"label": rule.label} if rule.label is not None else {}),
            }
        )

    closed_out: list[str] = []
    seen_closed: set[date] = set()
    for day in payload.closed_dates:
        if day in seen_closed:
            raise InvalidPeriodError(f"duplicate closed date: {day.isoformat()}")
        seen_closed.add(day)
        closed_out.append(day.isoformat())

    makeup_out = []
    seen_makeup: set[tuple] = set()
    for rule in payload.makeup:
        start = _parse_hhmm(rule.start_time, "start_time")
        end = _parse_hhmm(rule.end_time, "end_time")
        if end <= start:
            raise InvalidPeriodError(
                f"makeup opening on {rule.day.isoformat()} must be same-day with "
                f"end_time strictly after start_time, got {start.isoformat()}-"
                f"{end.isoformat()}"
            )
        key = (rule.day, start.isoformat(), end.isoformat())
        if key in seen_makeup:
            raise InvalidPeriodError(
                f"duplicate makeup rule on {rule.day.isoformat()}: "
                f"{start.isoformat()}-{end.isoformat()}"
            )
        seen_makeup.add(key)
        makeup_out.append(
            {
                "day": rule.day.isoformat(),
                "start_time": start.strftime("%H:%M"),
                "end_time": end.strftime("%H:%M"),
                **({"label": rule.label} if rule.label is not None else {}),
            }
        )

    return {
        "weekly": weekly_out,
        "closed_dates": closed_out,
        "makeup": makeup_out,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class PointOut(BaseModel):
    id: int
    name: str
    timezone: str
    version: int
    created_at: datetime
    updated_at: datetime


class IntervalOut(BaseModel):
    start: str
    end: str
    sources: list[dict]


class DayOut(BaseModel):
    local_date: date
    intervals: list[IntervalOut]


class CalendarOut(BaseModel):
    point_id: int
    version: int
    timezone: str
    date_from: date
    date_to: date
    days: list[DayOut]


class PublishOut(BaseModel):
    point_id: int
    version: int
    published_at: datetime


class VersionSummaryOut(BaseModel):
    version: int
    published_at: datetime


class VersionOut(BaseModel):
    point_id: int
    version: int
    published_at: datetime
    rules: dict


class AppointmentOut(BaseModel):
    id: int
    point_id: int
    start_at: str
    end_at: str
    idempotency_key: str
    cancel_token: str
    status: str
    rules_version: int
    created_at: datetime
    cancelled_at: datetime | None = None

    @field_serializer("created_at", "cancelled_at")
    def _serialize_dt(self, value: datetime | None) -> str | None:
        return None if value is None else utc_iso(value)
