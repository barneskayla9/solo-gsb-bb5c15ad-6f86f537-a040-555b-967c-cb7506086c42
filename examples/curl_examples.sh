#!/usr/bin/env bash
# End-to-end usage example for the recycling-point service calendar API.
# Requires: bash, curl.  Assumes the API is up (default: http://localhost:8000).
#
#   ./examples/curl_examples.sh                 # uses BASE_URL below
#   BASE_URL=http://localhost:8000 ./examples/curl_examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
api() { curl -sS -X "$1" "${BASE_URL}$2" ${3:+-H 'Content-Type: application/json' -d "$3"}; echo; }

echo "== 1. Create a recycling point (IANA timezone) =="
POINT_JSON="$(api POST /points '{"name":"阳光社区回收点","timezone":"Asia/Shanghai"}')"
echo "$POINT_JSON"
PID="$(echo "$POINT_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
echo "point id = $PID"

echo
echo "== 2. Publish rules v1 (optimistic on expected_version=0) =="
api POST "/points/$PID/rules" '{
  "expected_version": 0,
  "rules": {
    "weekly": [
      {"weekday": 1, "start_time": "09:00", "end_time": "17:00"},
      {"weekday": 3, "start_time": "09:00", "end_time": "17:00"},
      {"weekday": 5, "start_time": "20:00", "end_time": "02:00"}
    ],
    "closed_dates": ["2026-10-01"],
    "makeup": [
      {"day": "2026-10-01", "start_time": "13:00", "end_time": "16:00"}
    ]
  }
}'

echo
echo "== 3. Re-publishing with the stale expected_version 0 conflicts =="
api POST "/points/$PID/rules" '{
  "expected_version": 0,
  "rules": {"weekly": [], "closed_dates": [], "makeup": []}
}'

echo
echo "== 4. Query the calendar (inclusive local dates, both ends included) =="
api GET "/points/$PID/calendar?from=2026-09-28&to=2026-10-04"

echo
echo "== 5. Query pinned to the published version 1 =="
api GET "/points/$PID/calendar?from=2026-09-28&to=2026-10-04&version=1"

echo
echo "== 6. List and inspect published versions =="
api GET "/points/$PID/rules/versions"
api GET "/points/$PID/rules/versions/1"

echo
echo "== Error examples =="
echo "-- unknown timezone --"
api POST /points '{"name":"bad","timezone":"Mars/Olympus"}'
echo "-- invalid period (weekly zero length) --"
api POST "/points/$PID/rules" '{"expected_version":1,"rules":{
  "weekly":[{"weekday":2,"start_time":"10:00","end_time":"10:00"}],
  "closed_dates":[],"makeup":[]}}'
echo "-- no published rules (create another point) --"
EMPTY="$(api POST /points '{"name":"未配置点","timezone":"UTC"}')"
EPID="$(echo "$EMPTY" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
api GET "/points/$EPID/calendar?from=2026-09-28&to=2026-10-04"
