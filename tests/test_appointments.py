"""End-to-end tests for visit appointments."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db
from app import appointments as appt_domain
from app.errors import ApiError

ISO = "%Y-%m-%dT%H:%M:%SZ"


def create_point(client, name="预约回收点", tz="UTC"):
    r = client.post("/points", json={"name": name, "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()


def publish(client, pid, rules, expected_version=0):
    r = client.post(
        f"/points/{pid}/rules",
        json={"expected_version": expected_version, "rules": rules},
    )
    assert r.status_code == 201, r.text
    return r.json()


def daily_open_rules(start="00:00", end="23:00", weekdays=range(1, 8)):
    return {
        "weekly": [
            {"weekday": d, "start_time": start, "end_time": end} for d in weekdays
        ],
        "closed_dates": [],
        "makeup": [],
    }


def future_slot(days=2, hour=12, minute=0, duration_min=30):
    now = datetime.now(timezone.utc)
    base = (now + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return base, base + timedelta(minutes=duration_min)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime(ISO)


def book(client, pid, start, end, key, **extra):
    body = {"start_at": iso(start), "end_at": iso(end), "idempotency_key": key}
    body.update(extra)
    return client.post(f"/points/{pid}/appointments", json=body)


# ---------------------------------------------------------------------------
# Booking success / idempotency
# ---------------------------------------------------------------------------

def test_book_returns_number_credential_and_rule_version(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot()

    r = book(client, point["id"], start, end, "k-1")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "booked"
    assert isinstance(out["id"], int)
    assert out["rules_version"] == 1
    assert out["point_id"] == point["id"]
    assert out["start_at"] == iso(start) and out["end_at"] == iso(end)
    assert out["idempotency_key"] == "k-1"
    assert len(out["cancel_token"]) >= 20
    assert out["cancelled_at"] is None


def test_same_key_same_content_returns_original_result(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot()

    first = book(client, point["id"], start, end, "dedupe")
    assert first.status_code == 201
    original = first.json()

    second = book(client, point["id"], start, end, "dedupe")
    assert second.status_code == 201, second.text
    again = second.json()
    assert again["id"] == original["id"]
    assert again["cancel_token"] == original["cancel_token"]
    assert again["rules_version"] == original["rules_version"]


def test_same_key_different_content_conflicts(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot()
    first = book(client, point["id"], start, end, "reused")
    assert first.status_code == 201

    r = book(
        client, point["id"], start + timedelta(hours=1),
        end + timedelta(hours=1), "reused",
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["appointment_id"] == first.json()["id"]


def test_same_key_replay_after_cancel_returns_original_cancelled(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot()
    created = book(client, point["id"], start, end, "keep").json()
    cr = client.post(
        f"/points/{point['id']}/appointments/{created['id']}/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    assert cr.status_code == 200
    replay = book(client, point["id"], start, end, "keep").json()
    assert replay["id"] == created["id"]
    assert replay["status"] == "cancelled"
    assert replay["cancelled_at"] is not None


# ---------------------------------------------------------------------------
# Overlap semantics
# ---------------------------------------------------------------------------

def test_overlap_conflicts_but_touching_ranges_coexist(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot(hour=10)  # 10:00-10:30

    a = book(client, point["id"], start, end, "a").json()
    overlap = book(
        client, point["id"], start + timedelta(minutes=15),
        end + timedelta(minutes=15), "b",
    )
    assert overlap.status_code == 409
    assert overlap.json()["error"]["code"] == "appointment_conflict"
    assert overlap.json()["error"]["details"]["conflicting_appointment"] == a["id"]

    # End == next start: coexists.
    touch_after = book(client, point["id"], end, end + timedelta(minutes=30), "c")
    assert touch_after.status_code == 201, touch_after.text
    # Start == previous end: coexists too.
    touch_before = book(
        client, point["id"], start - timedelta(minutes=30), start, "d"
    )
    assert touch_before.status_code == 201, touch_before.text


def test_cancelled_appointment_frees_its_slot(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot(hour=10)
    first = book(client, point["id"], start, end, "x").json()
    client.post(
        f"/points/{point['id']}/appointments/{first['id']}/cancel",
        json={"cancel_token": first["cancel_token"]},
    )
    r = book(client, point["id"], start, end, "y")
    assert r.status_code == 201, r.text
    assert r.json()["id"] != first["id"]


def test_same_utc_range_at_different_points_does_not_conflict(client):
    p1 = create_point(client, name="点一")
    p2 = create_point(client, name="点二")
    publish(client, p1["id"], daily_open_rules())
    publish(client, p2["id"], daily_open_rules())
    start, end = future_slot(hour=11)
    assert book(client, p1["id"], start, end, "k").status_code == 201
    assert book(client, p2["id"], start, end, "k").status_code == 201


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_with_token_and_repeat_is_idempotent(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot()
    appt = book(client, point["id"], start, end, "cancel-me").json()

    r = client.post(
        f"/points/{point['id']}/appointments/{appt['id']}/cancel",
        json={"cancel_token": appt["cancel_token"]},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "cancelled"
    assert out["id"] == appt["id"]
    assert out["cancelled_at"] is not None

    again = client.post(
        f"/points/{point['id']}/appointments/{appt['id']}/cancel",
        json={"cancel_token": appt["cancel_token"]},
    )
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


def test_cancel_error_cases(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot()
    appt = book(client, point["id"], start, end, "cred").json()
    path = f"/points/{point['id']}/appointments/{appt['id']}/cancel"

    bad = client.post(path, json={"cancel_token": "wrong-token"})
    assert bad.status_code == 403
    assert bad.json()["error"]["code"] == "invalid_cancel_token"

    missing = client.post(path, json={})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_request"

    unknown = client.post(
        f"/points/{point['id']}/appointments/99999999/cancel",
        json={"cancel_token": "x"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "appointment_not_found"

    # The wrong token must not have cancelled it.
    assert (
        client.post(path, json={"cancel_token": appt["cancel_token"]}).json()[
            "status"
        ]
        == "cancelled"
    )


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "start_dt,end_dt,code",
    [
        ("end_before_start", None, "invalid_period"),
        ("too_long", None, "invalid_period"),
        ("in_past", None, "invalid_period"),
        ("too_far", None, "invalid_period"),
    ],
)
def test_booking_period_rules(client, start_dt, end_dt, code):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    now = datetime.now(timezone.utc)

    if start_dt == "end_before_start":
        s, _ = future_slot(hour=10)
        payload = {"start_at": iso(s), "end_at": iso(s - timedelta(minutes=30)),
                   "idempotency_key": "v"}
    elif start_dt == "too_long":
        s, _ = future_slot(hour=10)
        payload = {"start_at": iso(s), "end_at": iso(s + timedelta(hours=2, minutes=30)),
                   "idempotency_key": "v"}
    elif start_dt == "in_past":
        payload = {"start_at": iso(now - timedelta(hours=1)),
                   "end_at": iso(now + timedelta(minutes=30)),
                   "idempotency_key": "v"}
    else:
        s = now + timedelta(days=31)
        payload = {"start_at": iso(s), "end_at": iso(s + timedelta(minutes=30)),
                   "idempotency_key": "v"}

    r = client.post(f"/points/{point['id']}/appointments", json=payload)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


def test_naive_timestamp_is_invalid_request(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    r = client.post(
        f"/points/{point['id']}/appointments",
        json={"start_at": "2026-10-10T10:00:00",
              "end_at": "2026-10-10T10:30:00",
              "idempotency_key": "naive"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_booking_without_rules_returns_no_published_version(client):
    point = create_point(client, name="无规则点")
    start, end = future_slot()
    r = book(client, point["id"], start, end, "k")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_published_version"


def test_booking_unknown_point_404(client):
    start, end = future_slot()
    r = book(client, 99999999, start, end, "k")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "point_not_found"


# ---------------------------------------------------------------------------
# Open-hours containment
# ---------------------------------------------------------------------------

def test_appointment_must_fully_fit_one_continuous_interval(client):
    point = create_point(client)
    now = datetime.now(timezone.utc)
    target = (now + timedelta(days=2)).date()
    wd = target.isoweekday()
    publish(
        client,
        point["id"],
        {
            "weekly": [{"weekday": wd, "start_time": "10:00", "end_time": "12:00"}],
            "closed_dates": [],
            "makeup": [],
        },
    )

    def at(h1, m1, h2, m2):
        s = datetime.combine(target, datetime.min.time()).replace(
            hour=h1, minute=m1, tzinfo=timezone.utc
        )
        return s, s.replace(hour=h2, minute=m2)

    # Exactly at the boundaries: valid.
    s, e = at(10, 0, 11, 0)
    assert book(client, point["id"], s, e, "inside").status_code == 201
    # Starts before opening.
    s, e = at(9, 30, 10, 30)
    r = book(client, point["id"], s, e, "early")
    assert r.status_code == 422 and r.json()["error"]["code"] == "outside_open_hours"
    # Ends after closing.
    s, e = at(11, 30, 12, 30)
    r = book(client, point["id"], s, e, "late")
    assert r.status_code == 422 and r.json()["error"]["code"] == "outside_open_hours"
    # Same wall-clock on a different day: closed.
    other = datetime.combine(
        target + timedelta(days=1), datetime.min.time()
    ).replace(hour=10, minute=30, tzinfo=timezone.utc)
    r = book(client, point["id"], other, other + timedelta(minutes=30), "otherday")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "outside_open_hours"


def test_gap_between_open_intervals_is_not_continuous(client):
    # Open 00:00-23:00 daily: 23:00-24:00 is a gap; crossing it is invalid.
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, _ = future_slot(hour=22, minute=30)
    end = start + timedelta(minutes=45)  # crosses the 23:00 close
    r = book(client, point["id"], start, end, "cross-gap")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "outside_open_hours"


def test_overnight_appointment_may_cross_local_midnight(client):
    point = create_point(client, name="跨午夜", tz="Asia/Shanghai")
    # Friday 22:00 local -> Saturday 02:00 local.
    publish(
        client,
        point["id"],
        {
            "weekly": [{"weekday": 5, "start_time": "22:00", "end_time": "02:00"}],
            "closed_dates": [],
            "makeup": [],
        },
    )
    sh = ZoneInfo("Asia/Shanghai")
    today = datetime.now(sh).date()
    friday = today
    while friday.isoweekday() != 5:
        friday += timedelta(days=1)
    if (friday - today).days < 2:
        friday += timedelta(days=7)
    # A 2-hour window fully inside the 22:00 -> next-day 02:00 session.
    start = datetime.combine(friday, datetime.min.time()).replace(
        hour=23, tzinfo=sh
    ).astimezone(timezone.utc)
    end = datetime.combine(
        friday + timedelta(days=1), datetime.min.time()
    ).replace(hour=1, tzinfo=sh).astimezone(timezone.utc)

    r = book(client, point["id"], start, end, "overnight")
    assert r.status_code == 201, r.text
    assert r.json()["rules_version"] == 1

    # 15 minutes earlier, ending at the session start: only touches it, so it
    # is not contained -- whether open hours or an existing booking rejects it,
    # the range must not be accepted.
    early = book(
        client, point["id"],
        start - timedelta(minutes=15),
        end - timedelta(minutes=15),
        "overnight-bad",
    )
    assert early.status_code in (422, 409)
    assert early.json()["error"]["code"] in (
        "outside_open_hours",
        "appointment_conflict",
    )


# ---------------------------------------------------------------------------
# Rule / timezone changes vs. future appointments
# ---------------------------------------------------------------------------

def test_merged_open_intervals_handle_dst_gap_and_fall_back():
    # The booking windows are bounded by the 30-day lead limit, so the DST
    # calendar arithmetic is exercised at the domain level against the known
    # 2026 transition dates (the HTTP layer adds no date logic on top).
    import datetime as dtmod
    from app.scheduler import RuleSet, WeeklyRule

    # Cuba spring-forward at midnight: 00:00-06:00 local -> 05:00-10:00Z.
    havana = RuleSet(
        weekly=(WeeklyRule(7, dtmod.time(0), dtmod.time(6)),),
        closed_dates=frozenset(), makeup=(),
    )
    gap = appt_domain.merged_open_intervals(
        "America/Havana", havana,
        datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
        datetime(2026, 3, 8, 10, tzinfo=timezone.utc),
    )
    assert gap == [
        (datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
         datetime(2026, 3, 8, 10, tzinfo=timezone.utc))
    ]

    # New York fall-back 2026-11-01: 00:00-03:00 wall spans the repeated hour.
    ny = RuleSet(
        weekly=(WeeklyRule(7, dtmod.time(0), dtmod.time(3)),),
        closed_dates=frozenset(), makeup=(),
    )
    fallback = appt_domain.merged_open_intervals(
        "America/New_York", ny,
        datetime(2026, 11, 1, 4, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 8, tzinfo=timezone.utc),
    )
    assert fallback == [
        (datetime(2026, 11, 1, 4, tzinfo=timezone.utc),
         datetime(2026, 11, 1, 8, tzinfo=timezone.utc))
    ]
    assert appt_domain.is_fully_open(
        fallback,
        datetime(2026, 11, 1, 5, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
    )


def test_publishing_rules_that_strand_appointments_is_rejected(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot(hour=10)
    appt = book(client, point["id"], start, end, "locked").json()

    blocked = client.post(
        f"/points/{point['id']}/rules",
        json={"expected_version": 1, "rules": {"weekly": [], "closed_dates": [],
                                               "makeup": []}},
    )
    assert blocked.status_code == 409
    err = blocked.json()["error"]
    assert err["code"] == "appointment_rule_conflict"
    assert appt["id"] in err["details"]["conflicting_appointments"]
    # Version must not have advanced.
    assert client.get(f"/points/{point['id']}").json()["version"] == 1

    # A calendar that keeps the slot open publishes fine.
    ok = client.post(
        f"/points/{point['id']}/rules",
        json={"expected_version": 1, "rules": daily_open_rules("00:00", "23:59")},
    )
    assert ok.status_code == 201, ok.text

    # Once the appointment is cancelled, the previously blocked empty set
    # publishes.
    client.post(
        f"/points/{point['id']}/appointments/{appt['id']}/cancel",
        json={"cancel_token": appt["cancel_token"]},
    )
    gone = client.post(
        f"/points/{point['id']}/rules",
        json={"expected_version": 2, "rules": {"weekly": [], "closed_dates": [],
                                               "makeup": []}},
    )
    assert gone.status_code == 201, gone.text


def test_timezone_change_stranding_appointments_is_rejected(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())  # UTC 00:00-23:00
    # 15:00Z sits in the Shanghai daily gap (23:00-24:00 local) but is open now.
    start, end = future_slot(hour=15)
    appt = book(client, point["id"], start, end, "tz-locked").json()

    blocked = client.patch(
        f"/points/{point['id']}", json={"timezone": "Asia/Shanghai"}
    )
    assert blocked.status_code == 409
    err = blocked.json()["error"]
    assert err["code"] == "appointment_rule_conflict"
    assert err["details"]["conflicting_appointments"] == [appt["id"]]
    assert client.get(f"/points/{point['id']}").json()["timezone"] == "UTC"

    # Cancel, then the timezone change goes through.
    client.post(
        f"/points/{point['id']}/appointments/{appt['id']}/cancel",
        json={"cancel_token": appt["cancel_token"]},
    )
    ok = client.patch(f"/points/{point['id']}", json={"timezone": "Asia/Shanghai"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["timezone"] == "Asia/Shanghai"


def test_timezone_change_keeping_appointments_open_succeeds(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    start, end = future_slot(hour=12)  # 20:00 local Shanghai, inside 00-23
    assert book(client, point["id"], start, end, "tz-ok").status_code == 201
    r = client.patch(f"/points/{point['id']}", json={"timezone": "Asia/Shanghai"})
    assert r.status_code == 200, r.text
    assert r.json()["timezone"] == "Asia/Shanghai"


# ---------------------------------------------------------------------------
# Concurrency at the database layer
# ---------------------------------------------------------------------------

def test_concurrent_overlapping_bookings_exactly_one_wins(client):
    point = create_point(client)
    publish(client, point["id"], daily_open_rules())
    pid = point["id"]
    start, end = future_slot(hour=8)
    with db.conn() as conn:
        row = conn.execute(
            "SELECT payload FROM rule_versions WHERE point_id=%s ORDER BY version DESC LIMIT 1",
            (pid,),
        ).fetchone()
    ruleset = db.payload_to_ruleset(row["payload"])
    intervals = appt_domain.merged_open_intervals("UTC", ruleset, start, end)

    outcomes: list = []
    barrier = threading.Barrier(2)

    def attempt(key, s, e):
        barrier.wait()
        try:
            with db.conn() as connection:
                with connection.transaction():
                    cur = connection.cursor()
                    point = cur.execute(
                        "SELECT id, calendar_epoch FROM points "
                        "WHERE id=%s FOR UPDATE", (pid,)
                    ).fetchone()
                    db.create_appointment(
                        cur, pid, s, e, key, 1, point["calendar_epoch"], intervals
                    )
            outcomes.append(("ok", key))
        except ApiError as exc:
            outcomes.append((exc.code, key))

    threads = [
        threading.Thread(target=attempt, args=("race-a", start, end)),
        threading.Thread(
            target=attempt,
            args=("race-b", start + timedelta(minutes=10), end + timedelta(minutes=10)),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(code for code, _ in outcomes) == [
        "appointment_conflict",
        "ok",
    ]


def test_concurrent_booking_and_rule_change_cannot_both_break_invariant(client):
    # Per round: a fresh point with a daily calendar; one thread books a slot,
    # the other republishes an empty calendar.  Whatever the interleaving,
    # exactly one side wins -- a committed booking is never stranded by a
    # committed change.
    for round_no in range(6):
        point = create_point(client, name=f"并发-{round_no}")
        pid = point["id"]
        publish(client, pid, daily_open_rules())
        start, end = future_slot(days=3 + round_no, hour=9)

        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def booker():
            barrier.wait()
            try:
                with db.conn() as connection:
                    # READ COMMITTED, as used by the HTTP retry loop: once the
                    # lock is acquired the current epoch and version are read
                    # fresh, so a change that committed first is observed.
                    with connection.transaction():
                        cur = connection.cursor()
                        locked = cur.execute(
                            "SELECT version, calendar_epoch FROM points "
                            "WHERE id=%s FOR UPDATE",
                            (pid,),
                        ).fetchone()
                        version_row = cur.execute(
                            "SELECT version, payload FROM rule_versions "
                            "WHERE point_id=%s ORDER BY version DESC LIMIT 1",
                            (pid,),
                        ).fetchone()
                        current_ruleset = db.payload_to_ruleset(
                            version_row["payload"]
                        )
                        current_intervals = appt_domain.merged_open_intervals(
                            "UTC", current_ruleset, start, end
                        )
                        db.create_appointment(
                            cur, pid, start, end, f"race-{round_no}",
                            version_row["version"], locked["calendar_epoch"],
                            current_intervals,
                        )
                results["book"] = "ok"
            except ApiError as exc:
                results["book"] = exc.code

        def changer():
            barrier.wait()
            try:
                with db.conn() as connection:
                    with connection.transaction():
                        db.publish_rules(
                            connection.cursor(),
                            pid,
                            1,
                            {"weekly": [], "closed_dates": [], "makeup": []},
                        )
                results["change"] = "ok"
            except ApiError as exc:
                results["change"] = exc.code

        threads = [threading.Thread(target=booker), threading.Thread(target=changer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.get("book") is not None and results.get("change") is not None
        assert not (
            results["book"] == "ok" and results["change"] == "ok"
        ), results
        # No side may fail with an unexpected error.
        if results["book"] != "ok":
            assert results["book"] in (
                "outside_open_hours",
                "appointment_rule_conflict",
                "calendar_changed",
            ), results
        if results["change"] != "ok":
            assert results["change"] in (
                "version_conflict",
                "appointment_rule_conflict",
            ), results
