"""PostgreSQL access: pool lifecycle, schema init and rule repositories."""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path
from types import TracebackType

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .errors import (
    NotFoundError,
    NoPublishedVersionError,
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
    assignments = [f"{key} = %s" for key in fields]
    assignments.append("updated_at = now()")
    params = list(fields.values()) + [point_id]
    row = cur.execute(
        f"UPDATE points SET {', '.join(assignments)} WHERE id = %s "
        f"RETURNING id, name, timezone, version, created_at, updated_at",
        params,
    ).fetchone()
    if row is None:
        raise NotFoundError(f"recycling point {point_id} does not exist")
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
    cur.execute(
        "INSERT INTO rule_versions (point_id, version, payload) "
        "VALUES (%s, %s, %s::jsonb)",
        (point_id, next_version, psycopg.types.json.Json(payload)),
    )
    cur.execute(
        "UPDATE points SET version = %s, updated_at = now() WHERE id = %s",
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
