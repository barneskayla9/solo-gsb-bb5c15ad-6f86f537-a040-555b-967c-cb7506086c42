#!/usr/bin/env python3
"""Python (stdlib-only) calling example for the recycling calendar API.

Run with the API already started::

    python examples/python_client.py
    BASE_URL=http://localhost:8000 python examples/python_client.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n== {title} [{status}] ==")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    status, point = call(
        "POST",
        "/points",
        {"name": "阳光社区回收点", "timezone": "Asia/Shanghai"},
    )
    show("create point", status, point)
    pid = point["id"]

    status, published = call(
        "POST",
        f"/points/{pid}/rules",
        {
            "expected_version": 0,
            "rules": {
                # Open every day 00:00-23:00 local (UTC here): convenient for
                # appointment demos; closed_dates and makeup work the same.
                "weekly": [
                    {"weekday": d, "start_time": "00:00", "end_time": "23:00"}
                    for d in range(1, 8)
                ],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    show("publish v1", status, published)

    status, conflict = call(
        "POST",
        f"/points/{pid}/rules",
        {
            "expected_version": 0,  # stale
            "rules": {"weekly": [], "closed_dates": [], "makeup": []},
        },
    )
    show("stale publish -> distinguishable conflict", status, conflict)

    status, calendar = call(
        "GET", f"/points/{pid}/calendar?from=2026-09-28&to=2026-10-04"
    )
    show("calendar for an inclusive local-date range", status, calendar)

    # Compact rendering of the open intervals.
    if status == 200:
        print("\nOpen intervals (UTC):")
        for day in calendar["days"]:
            for interval in day["intervals"]:
                sources = ",".join(
                    s["kind"] + (f":{s.get('weekday') or s.get('day')}")
                    for s in interval["sources"]
                )
                print(
                    f"  {day['local_date']}  {interval['start']} -> {interval['end']}"
                    f"  [{sources}]"
                )

    # ------------------------------------------------------------------
    # Visit appointments
    # ------------------------------------------------------------------
    # A 30-minute slot two days from now at 12:00 UTC is inside the daily
    # 00:00-23:00 opening.
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(minutes=30)
    booking_body = {
        "start_at": utc(start),
        "end_at": utc(end),
        "idempotency_key": "demo-visit-0001",
    }

    status, booked = call("POST", f"/points/{pid}/appointments", booking_body)
    show("book a visit (appointment no + cancel credential)", status, booked)

    # Same key, same body -> the original result is returned unchanged.
    status, replay = call("POST", f"/points/{pid}/appointments", booking_body)
    show("retry with the same key/body -> original result", status, replay)

    # Same key, different time range -> explicit conflict.
    changed = dict(booking_body, start_at=utc(start + timedelta(hours=1)),
                   end_at=utc(end + timedelta(hours=1)))
    status, key_conflict = call("POST", f"/points/{pid}/appointments", changed)
    show("same key, different content -> 409", status, key_conflict)

    # Another visit touching the first one (end == next start) coexists.
    touching = {
        "start_at": utc(end),
        "end_at": utc(end + timedelta(minutes=30)),
        "idempotency_key": "demo-visit-0002",
    }
    status, second = call("POST", f"/points/{pid}/appointments", touching)
    show("back-to-back visit (touching is allowed)", status, second)

    # A true overlap is rejected.
    overlap = {
        "start_at": utc(start + timedelta(minutes=10)),
        "end_at": utc(end + timedelta(minutes=10)),
        "idempotency_key": "demo-visit-0003",
    }
    status, overlap_err = call("POST", f"/points/{pid}/appointments", overlap)
    show("overlapping visit -> 409", status, overlap_err)

    # Cancel with the credential; repeating it returns the cancelled state.
    if booked.get("id"):
        cancel_path = f"/points/{pid}/appointments/{booked['id']}/cancel"
        status, cancelled = call(
            "POST", cancel_path, {"cancel_token": booked["cancel_token"]}
        )
        show("cancel with credential", status, cancelled)
        status, again = call(
            "POST", cancel_path, {"cancel_token": booked["cancel_token"]}
        )
        show("repeat cancel -> still cancelled (idempotent)", status, again)
        status, bad_token = call(
            "POST", cancel_path, {"cancel_token": "wrong"}
        )
        show("cancel with wrong credential -> 403", status, bad_token)


if __name__ == "__main__":
    main()
