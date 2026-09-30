"""PostgreSQL access: pool lifecycle, schema init and rule repositories."""

from __future__ import annotations

import secrets
import time
from datetime import date, datetime
from pathlib import Path
from types import TracebackType

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import appointments as appt_domain
from .errors import (
    AppointmentConflictError,
    AppointmentNotFoundError,
    AppointmentRuleConflictError,
    CalendarChangedError,
    IdempotencyConflictError,
    InvalidCancelTokenError,
    NotFoundError,
    NoPublishedVersionError,
    OutsideOpenHoursError,
    VersionConflictError,
    VersionNotFoundError,
)
from .scheduler import MakeupRule, RuleSet, WeeklyRule

_SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

pool: ConnectionPool | None = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    global pool
    if pool is None:
        pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True)
    return pool


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None


def init_db(dsn: str, *, retries: int = 30, delay: float = 2.0) -> None:
    """Wait for PostgreSQL, create the pool and initialize the schema."""
    conn: psycopg.Connection | None = None
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            conn = psycopg.connect(dsn, connect_timeout=5)
            break
        except psycopg.OperationalError as exc:
            last_exc = exc
            time.sleep(delay)
    if conn is None:
        raise RuntimeError(f"database not reachable: {last_exc}")
    try:
        with conn:
            conn.execute(_SCHEMA_SQL)
    finally:
        conn.close()
    open_pool(dsn)


class _ConnCtx:
    """Connection from the pool with transactional autocommit semantics."""

    def __init__(self) -> None:
        assert pool is not None, "database pool is not open"
        self._cm = pool.connection()

    def __enter__(self) -> psycopg.Connection:
        self.conn = self._cm.__enter__()
        self.conn.row_factory = dict_row
        return self.conn

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._cm.__exit__(exc_type, exc, tb)


def conn() -> _ConnCtx:
    return _ConnCtx()


# ---------------------------------------------------------------------------
# Rule set (de)serialization
# ---------------------------------------------------------------------------

def payload_to_ruleset(payload: dict) -> RuleSet:
    weekly = tuple(
        WeeklyRule(
            weekday=item["weekday"],
            start_time=datetime.strptime(item["start_time"], "%H:%M").time(),
            end_time=datetime.strptime(item["end_time"], "%H:%M").time(),
        )
        for item in payload.get("weekly", [])
    )
    closed = frozenset(
        date.fromisoformat(day) for day in payload.get("closed_dates", [])
    )
    makeup = tuple(
        MakeupRule(
            day=date.fromisoformat(item["day"]),
            start_time=datetime.strptime(item["start_time"], "%H:%M").time(),
            end_time=datetime.strptime(item["end_time"], "%H:%M").time(),
        )
        for item in payload.get("makeup", [])
    )
    return RuleSet(weekly=weekly, closed_dates=closed, makeup=makeup)


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

def create_point(cur: psycopg.Cursor, name: str, timezone: str) -> dict:
    row = cur.execute(
        "INSERT INTO points (name, timezone) VALUES (%s, %s) "
        "RETURNING id, name, timezone, version, created_at, updated_at",
        (name, timezone),
    ).fetchone()
    return row


def get_point(cur: psycopg.Cursor, point_id: int) -> dict:
    row = cur.execute(
        "SELECT id, name, timezone, version, created_at, updated_at "
        "FROM points WHERE id = %s",
        (point_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"recycling point {point_id} does not exist")
    return row


def update_point(cur: psycopg.Cursor, point_id: int, **fields) -> dict:
    # Lock the point so a concurrent booking/publish cannot pass its own
    # containment or overlap check against the pre-change state while the
    # timezone is being changed.
    point = cur.execute(
        "SELECT id, timezone FROM points WHERE id = %s FOR UPDATE",
        (point_id,),
    ).fetchone()
    if point is None:
        raise NotFoundError(f"recycling point {point_id} does not exist")

    if "timezone" in fields and fields["timezone"] != point["timezone"]:
        # The published weekly/makeup rules are wall-clock based: a timezone
        # change moves every future opening in UTC.  Active appointments that
        # have not yet ended must still be fully open afterwards.
        _assert_future_appointments_open(cur, point_id, fields["timezone"])

    assignments = [f"{key} = %s" for key in fields]
    if "timezone" in fields:
        assignments.append("calendar_epoch = calendar_epoch + 1")
    assignments.append("updated_at = now()")
    params = list(fields.values()) + [point_id]
    row = cur.execute(
        f"UPDATE points SET {', '.join(assignments)} WHERE id = %s "
        f"RETURNING id, name, timezone, version, created_at, updated_at",
        params,
    ).fetchone()
    return row


# ---------------------------------------------------------------------------
# Atomic, conditional rule publication
# ---------------------------------------------------------------------------

def publish_rules(
    cur: psycopg.Cursor, point_id: int, expected_version: int, payload: dict
) -> dict:
    """Conditionally publish a new version inside the caller's transaction.

    The point row is locked first so concurrent publish attempts for the same
    point serialize; a stale ``expected_version`` aborts the whole
    transaction with a 409 and reports the current version.
    """
    point = cur.execute(
        "SELECT id, version FROM points WHERE id = %s FOR UPDATE",
        (point_id,),
    ).fetchone()
    if point is None:
        raise NotFoundError(f"recycling point {point_id} does not exist")
    current = point["version"]
    if current != expected_version:
        raise VersionConflictError(
            f"expected version {expected_version} but point {point_id} is at "
            f"version {current}",
            details={"expected_version": expected_version, "current_version": current},
        )

    next_version = current + 1
    # A new calendar may move openings away from already-booked active
    # appointments.  Every future active appointment must stay fully open
    # under the candidate rule set; otherwise the publication is rejected and
    # the conflicting appointment numbers are reported.
    _assert_future_appointments_open(
        cur, point_id, None, candidate_payload=payload
    )

    cur.execute(
        "INSERT INTO rule_versions (point_id, version, payload) "
        "VALUES (%s, %s, %s::jsonb)",
        (point_id, next_version, psycopg.types.json.Json(payload)),
    )
    cur.execute(
        "UPDATE points SET version = %s, calendar_epoch = calendar_epoch + 1, "
        "updated_at = now() WHERE id = %s",
        (next_version, point_id),
    )
    row = cur.execute(
        "SELECT version, published_at FROM rule_versions "
        "WHERE point_id = %s AND version = %s",
        (point_id, next_version),
    ).fetchone()
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_current_rules(cur: psycopg.Cursor, point_id: int) -> tuple[dict, dict]:
    """Return ``(point, version_row)`` for the single published version.

    A consistent snapshot is used so a concurrent publish cannot make the
    point version and the loaded rules disagree.
    """
    point = get_point(cur, point_id)
    row = cur.execute(
        "SELECT version, payload, published_at FROM rule_versions "
        "WHERE point_id = %s ORDER BY version DESC LIMIT 1",
        (point_id,),
    ).fetchone()
    if row is None:
        raise NoPublishedVersionError(
            f"point {point_id} has no published rule set yet",
            details={"point_id": point_id, "current_version": point["version"]},
        )
    return point, row


def list_versions(cur: psycopg.Cursor, point_id: int) -> list[dict]:
    get_point(cur, point_id)
    return cur.execute(
        "SELECT version, published_at FROM rule_versions "
        "WHERE point_id = %s ORDER BY version DESC",
        (point_id,),
    ).fetchall()


def get_version(cur: psycopg.Cursor, point_id: int, version: int) -> dict:
    _point, row = get_pinned_rules(cur, point_id, version)
    return row


def get_pinned_rules(
    cur: psycopg.Cursor, point_id: int, version: int
) -> tuple[dict, dict]:
    """Return ``(point, version_row)`` for one specific published version."""
    point = get_point(cur, point_id)
    row = cur.execute(
        "SELECT version, payload, published_at FROM rule_versions "
        "WHERE point_id = %s AND version = %s",
        (point_id, version),
    ).fetchone()
    if row is None:
        raise VersionNotFoundError(
            f"version {version} of point {point_id} does not exist",
            details={"point_id": point_id, "requested_version": version},
        )
    return point, row


# ---------------------------------------------------------------------------
# Appointment guard shared by rule publication and timezone changes
# ---------------------------------------------------------------------------

def _future_active_appointments(cur: psycopg.Cursor, point_id: int) -> list[dict]:
    """Active appointments that have not yet started (UTC).

    An appointment already in progress or fully in the past is honoured as
    booked and never blocks a change.
    """
    return cur.execute(
        "SELECT id, start_at, end_at FROM appointments "
        "WHERE point_id = %s AND status = 'booked' AND start_at > now() "
        "ORDER BY start_at, id",
        (point_id,),
    ).fetchall()


def _assert_future_appointments_open(
    cur: psycopg.Cursor,
    point_id: int,
    tz_name: str | None,
    *,
    candidate_payload: dict | None = None,
) -> None:
    """Reject a rule/timezone change stranding future active appointments.

    Exactly one of ``tz_name`` (timezone change, rules stay at current
    version) or ``candidate_payload`` (publication, timezone stays current)
    is given.
    """
    rows = _future_active_appointments(cur, point_id)
    if not rows:
        return
    point = cur.execute(
        "SELECT timezone FROM points WHERE id = %s", (point_id,)
    ).fetchone()
    if candidate_payload is not None:
        zone_name = point["timezone"]
        ruleset = payload_to_ruleset(candidate_payload)
    else:
        zone_name = tz_name
        version_row = cur.execute(
            "SELECT payload FROM rule_versions "
            "WHERE point_id = %s ORDER BY version DESC LIMIT 1",
            (point_id,),
        ).fetchone()
        # Future appointments can only exist when a rule set was published.
        ruleset = payload_to_ruleset(version_row["payload"])
    conflicts = appt_domain.conflicting_future_appointments(
        zone_name, ruleset, rows
    )
    if conflicts:
        raise AppointmentRuleConflictError(
            f"the change would leave {len(conflicts)} future active "
            f"appointment(s) outside open hours at point {point_id}",
            details={"point_id": point_id, "conflicting_appointments": conflicts},
        )


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------

def find_idempotent_appointment(
    cur: psycopg.Cursor, point_id: int, idempotency_key: str
) -> dict | None:
    return cur.execute(
        "SELECT * FROM appointments WHERE point_id = %s AND idempotency_key = %s",
        (point_id, idempotency_key),
    ).fetchone()


def _active_overlap(
    cur: psycopg.Cursor, point_id: int, start_at: datetime, end_at: datetime
) -> dict | None:
    """An active appointment with a real UTC overlap; touching is allowed."""
    return cur.execute(
        "SELECT id, start_at, end_at FROM appointments "
        "WHERE point_id = %s AND status = 'booked' "
        "AND start_at < %s AND %s < end_at "
        "ORDER BY start_at LIMIT 1",
        (point_id, end_at, start_at),
    ).fetchone()


def create_appointment(
    cur: psycopg.Cursor,
    point_id: int,
    start_at: datetime,
    end_at: datetime,
    idempotency_key: str,
    rules_version: int,
    calendar_epoch: int,
    candidate_intervals: list[tuple[datetime, datetime]],
) -> dict:
    """Insert a booking inside the caller's transaction.

    The caller holds the point row lock (``FOR UPDATE``) and has validated the
    request against ``calendar_epoch``.  Re-reading the epoch here makes a
    calendar publication/timezone change that committed between the read and
    the lock abort the booking with a retryable conflict.  The overlap
    trigger/constraint is the last line of defence for direct SQL writes.
    """
    current_epoch = cur.execute(
        "SELECT calendar_epoch FROM points WHERE id = %s", (point_id,)
    ).fetchone()["calendar_epoch"]
    if current_epoch != calendar_epoch:
        raise CalendarChangedError(
            f"calendar of point {point_id} changed while booking; retry",
            details={
                "point_id": point_id,
                "seen_epoch": calendar_epoch,
                "current_epoch": current_epoch,
            },
        )

    # 1. Idempotent replay: first request for this key wins for good; a
    #    different body with the same key is a 409 conflict.
    existing = find_idempotent_appointment(cur, point_id, idempotency_key)
    if existing is not None:
        if (
            existing["start_at"] != start_at
            or existing["end_at"] != end_at
        ):
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} was already used at "
                f"point {point_id} with a different time range",
                details={
                    "point_id": point_id,
                    "idempotency_key": idempotency_key,
                    "appointment_id": existing["id"],
                    "original": {
                        "start_at": existing["start_at"].isoformat(),
                        "end_at": existing["end_at"].isoformat(),
                    },
                },
            )
        return existing

    # 2. Must sit entirely within one continuous open interval of the
    #    currently published calendar.
    if not appt_domain.is_fully_open(candidate_intervals, start_at, end_at):
        raise OutsideOpenHoursError(
            f"requested UTC range {start_at.isoformat()}..{end_at.isoformat()} "
            f"is not fully inside a continuous open interval of point {point_id}",
            details={
                "point_id": point_id,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
            },
        )

    # 3. No UTC overlap with another active appointment (friendly pre-check).
    overlap = _active_overlap(cur, point_id, start_at, end_at)
    if overlap is not None:
        raise AppointmentConflictError(
            f"requested range overlaps active appointment {overlap['id']} "
            f"at point {point_id}",
            details={
                "point_id": point_id,
                "conflicting_appointment": overlap["id"],
            },
        )

    token = secrets.token_urlsafe(32)
    try:
        return cur.execute(
            "INSERT INTO appointments "
            "(point_id, start_at, end_at, idempotency_key, cancel_token, "
            " rules_version) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (
                point_id,
                start_at,
                end_at,
                idempotency_key,
                token,
                rules_version,
            ),
        ).fetchone()
    except (
        psycopg.errors.ExclusionViolation,
        psycopg.errors.RaiseException,
    ) as exc:
        # GiST EXCLUDE (deployments with btree_gist) or the plpgsql overlap
        # trigger always report the same business error to clients.
        raise AppointmentConflictError(
            f"requested range overlaps another active appointment at "
            f"point {point_id}",
            details={"point_id": point_id},
        ) from exc


def cancel_appointment(
    cur: psycopg.Cursor, point_id: int, appointment_id: int, cancel_token: str
) -> dict:
    row = cur.execute(
        "SELECT * FROM appointments WHERE id = %s AND point_id = %s FOR UPDATE",
        (appointment_id, point_id),
    ).fetchone()
    if row is None:
        raise AppointmentNotFoundError(
            f"appointment {appointment_id} does not exist at point {point_id}"
        )
    if not secrets.compare_digest(row["cancel_token"], cancel_token):
        raise InvalidCancelTokenError(
            f"cancel token does not match appointment {appointment_id}",
            details={"appointment_id": appointment_id},
        )
    if row["status"] != "cancelled":
        row = cur.execute(
            "UPDATE appointments SET status = 'cancelled', "
            "cancelled_at = now() WHERE id = %s RETURNING *",
            (appointment_id,),
        ).fetchone()
    # Repeated cancellation with the same valid token is idempotent: the
    # already-cancelled state (and its credential) is returned again.
    return row
