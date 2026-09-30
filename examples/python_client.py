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
                "weekly": [
                    {"weekday": 1, "start_time": "09:00", "end_time": "17:00"},
                    {"weekday": 3, "start_time": "09:00", "end_time": "17:00"},
                    {"weekday": 5, "start_time": "20:00", "end_time": "02:00"},
                ],
                "closed_dates": ["2026-10-01"],
                "makeup": [
                    {"day": "2026-10-01", "start_time": "13:00", "end_time": "16:00"}
                ],
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


if __name__ == "__main__":
    main()
