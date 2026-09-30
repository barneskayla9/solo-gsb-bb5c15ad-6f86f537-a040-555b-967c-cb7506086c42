#!/usr/bin/env bash
# End-to-end usage example for the recycling-point service calendar API.
# Requires: bash, curl.  Assumes the API is up (default: http://localhost:8000).
#
#   ./examples/curl_examples.sh                 # uses BASE_URL below
#   BASE_URL=http://localhost:8000 ./examples/curl_examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
api() { curl -sS -X "$1" "${BASE_URL}$2" ${3:+-H 'Content-Type: application/json' -d "$3"}; echo; }

# UTC timestamps for "the day after tomorrow, 12:00-12:30 UTC" — guaranteed to
# sit inside the daily 00:00-23:00 UTC opening published below.  Requires
# python3 for date math.
read -r T_START T_END T_END2 <<EOF
$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
d = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
    hour=12, minute=0, second=0, microsecond=0)
f = "%Y-%m-%dT%H:%M:%SZ"
print(d.strftime(f), (d + timedelta(minutes=30)).strftime(f),
      (d + timedelta(minutes=60)).strftime(f))
PY
)
EOF

echo "== 1. Create a recycling point (IANA timezone) =="
POINT_JSON="$(api POST /points '{"name":"阳光社区回收点","timezone":"UTC"}')"
echo "$POINT_JSON"
PID="$(echo "$POINT_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
echo "point id = $PID"

echo
echo "== 2. Publish rules v1 (daily 00:00-23:00, optimistic on expected_version=0) =="
api POST "/points/$PID/rules" '{
  "expected_version": 0,
  "rules": {
    "weekly": [
      {"weekday": 1, "start_time": "00:00", "end_time": "23:00"},
      {"weekday": 2, "start_time": "00:00", "end_time": "23:00"},
      {"weekday": 3, "start_time": "00:00", "end_time": "23:00"},
      {"weekday": 4, "start_time": "00:00", "end_time": "23:00"},
      {"weekday": 5, "start_time": "00:00", "end_time": "23:00"},
      {"weekday": 6, "start_time": "00:00", "end_time": "23:00"},
      {"weekday": 7, "start_time": "00:00", "end_time": "23:00"}
    ],
    "closed_dates": [],
    "makeup": []
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
echo "== 7. Book a visit appointment ($T_START .. $T_END UTC) =="
BOOK_JSON="$(api POST "/points/$PID/appointments" "{
  \"start_at\": \"$T_START\",
  \"end_at\": \"$T_END\",
  \"idempotency_key\": \"curl-demo-0001\"
}")"
echo "$BOOK_JSON"
APPT_ID="$(echo "$BOOK_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p' | head -1)"
TOKEN="$(echo "$BOOK_JSON" | sed -n 's/.*"cancel_token":"\([^"]*\)".*/\1/p')"
echo "appointment no = $APPT_ID"

echo
echo "== 8. Same key + same body: original result comes back =="
api POST "/points/$PID/appointments" "{
  \"start_at\": \"$T_START\",
  \"end_at\": \"$T_END\",
  \"idempotency_key\": \"curl-demo-0001\"
}"

echo
echo "== 9. Same key + different range: 409 idempotency_conflict =="
api POST "/points/$PID/appointments" "{
  \"start_at\": \"$T_END\",
  \"end_at\": \"$T_END2\",
  \"idempotency_key\": \"curl-demo-0001\"
}"

echo
echo "== 10. Touching (end == next start) is allowed; an overlap is not =="
api POST "/points/$PID/appointments" "{
  \"start_at\": \"$T_END\",
  \"end_at\": \"$T_END2\",
  \"idempotency_key\": \"curl-demo-0002\"
}"
api POST "/points/$PID/appointments" "{
  \"start_at\": \"$T_START\",
  \"end_at\": \"$T_END2\",
  \"idempotency_key\": \"curl-demo-0003\"
}"

echo
echo "== 11. Cancel with the credential (repeating returns the same state) =="
api POST "/points/$PID/appointments/$APPT_ID/cancel" "{\"cancel_token\":\"$TOKEN\"}"
api POST "/points/$PID/appointments/$APPT_ID/cancel" "{\"cancel_token\":\"$TOKEN\"}"
echo "-- wrong credential --"
api POST "/points/$PID/appointments/$APPT_ID/cancel" '{"cancel_token":"wrong"}'

echo
echo "== Error examples =="
echo "-- unknown timezone --"
api POST /points '{"name":"bad","timezone":"Mars/Olympus"}'
echo "-- invalid period (weekly zero length) --"
api POST "/points/$PID/rules" '{"expected_version":1,"rules":{
  "weekly":[{"weekday":2,"start_time":"10:00","end_time":"10:00"}],
  "closed_dates":[],"makeup":[]}}'
echo "-- publishing an empty calendar is blocked by future bookings --"
api POST "/points/$PID/rules" '{"expected_version":1,"rules":{
  "weekly":[],"closed_dates":[],"makeup":[]}}'
echo "-- booking without rules (create another point) --"
EMPTY="$(api POST /points '{"name":"未配置点","timezone":"UTC"}')"
EPID="$(echo "$EMPTY" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
api POST "/points/$EPID/appointments" "{
  \"start_at\": \"$T_START\", \"end_at\": \"$T_END\",
  \"idempotency_key\": \"no-rules\"
}"
api GET "/points/$EPID/calendar?from=2026-09-28&to=2026-10-04"
