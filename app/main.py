"""FastAPI application: recycling-point service calendar HTTP API."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import psycopg

from . import appointments as appt_domain
from . import db
from .config import settings
from .errors import (
    ApiError,
    CalendarChangedError,
    InvalidPeriodError,
    NoPublishedVersionError,
    NotFoundError,
)
from .schemas import (
    AppointmentCancelIn,
    AppointmentCreateIn,
    AppointmentOut,
    CalendarOut,
    DayOut,
    IntervalOut,
    PointCreateIn,
    PointOut,
    PointUpdateIn,
    PublishOut,
    PublishRulesIn,
    VersionOut,
    VersionSummaryOut,
    utc_iso,
    validate_rules_payload,
    validate_timezone,
)
from .scheduler import build_calendar
from .db import payload_to_ruleset


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(settings.database_url)
    yield
    db.close_pool()


app = FastAPI(
    title="Recycling Point Service Calendar API",
    version="1.1.0",
    description=(
        "Versioned weekly openings, full-day closures and same-day makeup "
        "openings for community recycling points, expanded to UTC intervals, "
        "plus visit appointments booked against the published calendar."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling — every error is a distinguishable JSON envelope
# ---------------------------------------------------------------------------

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic v2 embeds the original exception object in ``ctx.error``; that
    # object is not JSON serializable, so stringify anything exotic.
    def _safe(value):
        if isinstance(value, dict):
            return {k: _safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_safe(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": "request payload failed validation",
                "details": {"errors": _safe(exc.errors())},
            }
        },
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

@app.post("/points", response_model=PointOut, status_code=201, tags=["points"])
def create_point(body: PointCreateIn) -> PointOut:
    validate_timezone(body.timezone)
    with db.conn() as connection:
        with connection.transaction():
            row = db.create_point(connection.cursor(), body.name, body.timezone)
    return PointOut(**row)


@app.get("/points/{point_id}", response_model=PointOut, tags=["points"])
def read_point(point_id: int) -> PointOut:
    with db.conn() as connection:
        row = db.get_point(connection.cursor(), point_id)
    return PointOut(**row)


@app.patch("/points/{point_id}", response_model=PointOut, tags=["points"])
def patch_point(point_id: int, body: PointUpdateIn) -> PointOut:
    fields = body.model_dump(exclude_unset=True)
    if "timezone" in fields:
        validate_timezone(fields["timezone"])
    with db.conn() as connection:
        with connection.transaction():
            row = db.update_point(connection.cursor(), point_id, **fields)
    return PointOut(**row)


# ---------------------------------------------------------------------------
# Versioned rule sets
# ---------------------------------------------------------------------------

@app.post(
    "/points/{point_id}/rules",
    response_model=PublishOut,
    status_code=201,
    tags=["rules"],
)
def publish_rules(point_id: int, body: PublishRulesIn) -> PublishOut:
    # Semantic validation (invalid periods, duplicates) before touching the DB.
    payload = validate_rules_payload(body.rules)
    with db.conn() as connection:
        with connection.transaction():
            row = db.publish_rules(
                connection.cursor(), point_id, body.expected_version, payload
            )
    return PublishOut(
        point_id=point_id, version=row["version"], published_at=row["published_at"]
    )


@app.get(
    "/points/{point_id}/rules",
    response_model=VersionOut,
    tags=["rules"],
)
def read_current_rules(point_id: int) -> VersionOut:
    with db.conn() as connection:
        point, row = db.get_current_rules(connection.cursor(), point_id)
    return VersionOut(
        point_id=point_id,
        version=row["version"],
        published_at=row["published_at"],
        rules=row["payload"],
    )


@app.get(
    "/points/{point_id}/rules/versions",
    response_model=list[VersionSummaryOut],
    tags=["rules"],
)
def list_rule_versions(point_id: int) -> list[VersionSummaryOut]:
    with db.conn() as connection:
        rows = db.list_versions(connection.cursor(), point_id)
    return [VersionSummaryOut(**row) for row in rows]


@app.get(
    "/points/{point_id}/rules/versions/{version}",
    response_model=VersionOut,
    tags=["rules"],
)
def read_rule_version(point_id: int, version: int) -> VersionOut:
    with db.conn() as connection:
        row = db.get_version(connection.cursor(), point_id, version)
    return VersionOut(
        point_id=point_id,
        version=row["version"],
        published_at=row["published_at"],
        rules=row["payload"],
    )


# ---------------------------------------------------------------------------
# Calendar query — always against one published version
# ---------------------------------------------------------------------------

@app.get(
    "/points/{point_id}/calendar",
    response_model=CalendarOut,
    tags=["calendar"],
)
def calendar(
    point_id: int,
    date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
    version: int | None = Query(
        default=None,
        ge=1,
        description="Pin the query to one published version; defaults to latest.",
    ),
) -> CalendarOut:
    if date_to < date_from:
        raise InvalidPeriodError("'to' must be on or after 'from'")
    span = (date_to - date_from).days + 1
    if span > settings.max_query_days:
        raise InvalidPeriodError(
            f"date range spans {span} days; maximum is {settings.max_query_days}",
            details={"max_query_days": settings.max_query_days, "span_days": span},
        )
    with db.conn() as connection:
        # Repeatable-read snapshot: the point row and the loaded rule version
        # always agree, even if a concurrent publish commits mid-query.
        previous_level = connection.isolation_level
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        try:
            with connection.transaction():
                cur = connection.cursor()
                if version is None:
                    point, row = db.get_current_rules(cur, point_id)
                else:
                    point, row = db.get_pinned_rules(cur, point_id, version)
        finally:
            connection.isolation_level = previous_level
    used_version = row["version"]
    ruleset = payload_to_ruleset(row["payload"])
    days = build_calendar(point["timezone"], ruleset, date_from, date_to)
    return CalendarOut(
        point_id=point_id,
        version=used_version,
        timezone=point["timezone"],
        date_from=date_from,
        date_to=date_to,
        days=[
            DayOut(
                local_date=d.local_date,
                intervals=[
                    IntervalOut(
                        start=utc_iso(start),
                        end=utc_iso(end),
                        sources=[s.as_dict() for s in sources],
                    )
                    for start, end, sources in d.intervals
                ],
            )
            for d in days
        ],
    )


# ---------------------------------------------------------------------------
# Visit appointments — booked against the currently published calendar
# ---------------------------------------------------------------------------

def _appointment_out(row: dict) -> AppointmentOut:
    return AppointmentOut(
        id=row["id"],
        point_id=row["point_id"],
        start_at=utc_iso(row["start_at"]),
        end_at=utc_iso(row["end_at"]),
        idempotency_key=row["idempotency_key"],
        cancel_token=row["cancel_token"],
        status=row["status"],
        rules_version=row["rules_version"],
        created_at=row["created_at"],
        cancelled_at=row["cancelled_at"],
    )


@app.post(
    "/points/{point_id}/appointments",
    response_model=AppointmentOut,
    status_code=201,
    tags=["appointments"],
)
def create_appointment(point_id: int, body: AppointmentCreateIn) -> AppointmentOut:
    # Request-shape independent period rules.
    now = datetime.now(timezone.utc)
    max_lead = timedelta(days=settings.booking_max_lead_days)
    max_duration = timedelta(hours=settings.booking_max_duration_hours)
    if body.end_at <= body.start_at:
        raise InvalidPeriodError(
            "'end_at' must be strictly after 'start_at'",
            details={"start_at": body.start_at.isoformat(),
                     "end_at": body.end_at.isoformat()},
        )
    if body.end_at - body.start_at > max_duration:
        raise InvalidPeriodError(
            f"an appointment may last at most "
            f"{settings.booking_max_duration_hours} hours",
            details={"max_duration_hours": settings.booking_max_duration_hours},
        )
    if body.start_at <= now:
        raise InvalidPeriodError(
            "'start_at' must be in the future",
            details={"now": utc_iso(now)},
        )
    if body.start_at > now + max_lead:
        raise InvalidPeriodError(
            f"'start_at' must be within {settings.booking_max_lead_days} days "
            "from now",
            details={"max_lead_days": settings.booking_max_lead_days,
                     "now": utc_iso(now)},
        )

    # Point-row locking plus the calendar-epoch recheck and the overlap
    # trigger make concurrent bookings, rule publications and timezone
    # changes safe under REPEATABLE READ.  A calendar that changed between
    # the read and the lock aborts this attempt and is transparently retried
    # (the idempotency lookup keeps retries safe).
    def work(cur) -> dict:
        point = cur.execute(
            "SELECT id, timezone, version, calendar_epoch "
            "FROM points WHERE id = %s FOR UPDATE",
            (point_id,),
        ).fetchone()
        if point is None:
            raise NotFoundError(f"recycling point {point_id} does not exist")
        version_row = cur.execute(
            "SELECT version, payload FROM rule_versions "
            "WHERE point_id = %s ORDER BY version DESC LIMIT 1",
            (point_id,),
        ).fetchone()
        if version_row is None:
            raise NoPublishedVersionError(
                f"point {point_id} has no published rule set yet",
                details={"point_id": point_id, "current_version": point["version"]},
            )

        ruleset = payload_to_ruleset(version_row["payload"])
        intervals = appt_domain.merged_open_intervals(
            point["timezone"], ruleset, body.start_at, body.end_at
        )
        return db.create_appointment(
            cur,
            point_id,
            body.start_at,
            body.end_at,
            body.idempotency_key,
            version_row["version"],
            point["calendar_epoch"],
            intervals,
        )

    with db.conn() as connection:
        previous_level = connection.isolation_level
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        row = None
        try:
            for _ in range(5):
                try:
                    with connection.transaction():
                        row = work(connection.cursor())
                    break
                except CalendarChangedError:
                    continue
            if row is None:  # pragma: no cover - defensive
                raise CalendarChangedError(
                    f"calendar of point {point_id} kept changing while booking"
                )
        finally:
            connection.isolation_level = previous_level
    return _appointment_out(row)


@app.post(
    "/points/{point_id}/appointments/{appointment_id}/cancel",
    response_model=AppointmentOut,
    tags=["appointments"],
)
def cancel_appointment(
    point_id: int, appointment_id: int, body: AppointmentCancelIn
) -> AppointmentOut:
    with db.conn() as connection:
        with connection.transaction():
            row = db.cancel_appointment(
                connection.cursor(), point_id, appointment_id, body.cancel_token
            )
    return _appointment_out(row)
