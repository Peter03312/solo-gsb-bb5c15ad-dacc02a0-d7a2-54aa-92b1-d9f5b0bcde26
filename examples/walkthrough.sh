#!/usr/bin/env bash
# End-to-end curl walkthrough for the Temporary Event Radio Spectrum API.
# Usage: ./examples/walkthrough.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

echo "== health =="
curl -s "$BASE/health"; echo

echo "== 1. invalid submission (422, whole request rejected) =="
post /api/applications '{
  "name":"bad",
  "circle":{"cx":0,"cy":0,"radius":-5},
  "frequency":{"low":200,"high":100},
  "time_window":{"start":"2026-10-01T10:00:00Z","end":"2026-10-01T08:00:00Z"}
}' | J

echo "== 2. submit A =="
A=$(post /api/applications '{
  "name":"Festival A",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T12:00:00Z"}
}')
echo "$A" | J
A_ID=$(echo "$A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 3. preview A =="
post "/api/applications/$A_ID/preview" | J

echo "== 4. approve A at revision 0 =="
post "/api/applications/$A_ID/approve" '{"expected_revision":0}' | J

echo "== 5. submit B (1 MHz gap < 2 MHz guard band) =="
B=$(post /api/applications '{
  "name":"Marathon B",
  "circle":{"cx":100,"cy":0,"radius":1000},
  "frequency":{"low":111,"high":112},
  "time_window":{"start":"2026-10-01T09:00:00Z","end":"2026-10-01T11:00:00Z"}
}')
B_ID=$(echo "$B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$B_ID/preview" | J

echo "== 6. stale approval (expected 0, current 1) =="
post "/api/applications/$B_ID/approve" '{"expected_revision":0}' | J || true

echo "== 7. current revision but interference =="
post "/api/applications/$B_ID/approve" '{"expected_revision":1}' | J || true

echo "== 8. revoke A =="
post "/api/applications/$A_ID/revoke" '{"reason":"postponed"}' | J

echo "== 9. approve B at revision 2 =="
post "/api/applications/$B_ID/approve" '{"expected_revision":2}' | J

echo "== 10. history snapshots =="
curl -s "$BASE/api/revision/1" | J
curl -s "$BASE/api/revision/2" | J
curl -s "$BASE/api/revision/current" | J

echo "== 11. submit three more devices for one event (disjoint bands) =="
mk() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}"; }
C=$(mk "Device C" 200 210); C_ID=$(echo "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
D=$(mk "Device D" 300 310); D_ID=$(echo "$D" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
E=$(mk "Device E" 400 410); E_ID=$(echo "$E" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "ids: $C_ID $D_ID $E_ID"

echo "== 12. batch approve at revision 3, input order D,C,E -> revisions 4,5,6 =="
post /api/applications/batch-approve \
  "{\"expected_revision\":3,\"application_ids\":[$D_ID,$C_ID,$E_ID]}" | J

echo "== 13. snapshot as of the second item (revision 5) has D and C effective =="
curl -s "$BASE/api/revision/5" | J

echo "== 14. failing batch: two members clash with each other -> whole group rejected =="
F=$(mk "Device F" 500 510); F_ID=$(echo "$F" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G=$(mk "Device G" 505 515); G_ID=$(echo "$G" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
H=$(mk "Device H" 600 610); H_ID=$(echo "$H" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[$F_ID,$G_ID,$H_ID]}" | J || true
echo "revision is still 6 and all three are pending:"
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print("current_revision =", json.load(sys.stdin)["current_revision"])'

echo "== 15. malformed batches (duplicate ids / missing id) =="
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[$F_ID,$F_ID]}" | J || true
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[999999]}" | J || true

echo "== 16. guard band: current config (live value, initial value, revision) =="
curl -s "$BASE/api/config/guard-band" | J

echo "== 17. preview a wider band (100): pairs among the effective grants appear =="
echo "    (devices C/D/E sit 90 MHz apart; B's time window does not overlap theirs)"
post /api/config/guard-band/preview '{"guard_band":100}' | J

echo "== 18. confirming it is refused (409 interference); nothing changes =="
post /api/config/guard-band '{"guard_band":100,"expected_revision":6}' | J || true
curl -s "$BASE/api/config/guard-band" | J

echo "== 19. tighten to 1.0: accepted, exactly one new revision (7) =="
post /api/config/guard-band '{"guard_band":1.0,"expected_revision":6}' | J

echo "== 20. the change shares revision arbitration: stale confirms/approvals lose =="
post /api/config/guard-band '{"guard_band":0.5,"expected_revision":6}' | J || true
post "/api/applications/$F_ID/approve" '{"expected_revision":6}' | J || true

echo "== 21. invalid values are rejected with 422 (no revision consumed) =="
post /api/config/guard-band '{"guard_band":-5,"expected_revision":7}' | J || true

echo "== 22. history restores the band in force at each revision =="
curl -s "$BASE/api/revision/6" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revision", d["revision"], "guard_band =", d["guard_band"])'
curl -s "$BASE/api/revision/7" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revision", d["revision"], "guard_band =", d["guard_band"], "changes =", [(c["revision"], c["previous_guard_band"], c["guard_band"]) for c in d["guard_band_changes"]])'
echo "    approval events keep the band they were decided under (not rewritten):"
curl -s "$BASE/api/revision/7" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("approval guard_bands =", [(a["revision"], a["guard_band"]) for a in d["approvals"]])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "guard_band =", d["guard_band"])'

echo "== 23. available-ranges search for pending device F (band [500,510], width 10) =="
echo "    effective grants in its 13:00-18:00 window: C [200,210], D [300,310], E [400,410]; guard band is now 1.0"
post "/api/applications/$F_ID/available-ranges" \
  '{"candidate_low_min":180,"candidate_low_max":430}' | J
echo "    forbidden by C: (200-10-1, 210+1) = (189,211); by D: (289,311); by E: (389,411)"
echo "    -> free lower bounds: [180,189], [211,289], [311,389], [411,430] (endpoints attainable)"

echo "== 24. a reported endpoint previews conflict-free; a forbidden point does not =="
SHIFTED=$(post /api/applications "{
  \"name\":\"F shifted to 211\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":211,\"high\":221},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}")
SHIFTED_ID=$(echo "$SHIFTED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$SHIFTED_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("shifted to 211 (gap == guard): has_conflicts =", d["has_conflicts"])'
CLASH=$(post /api/applications "{
  \"name\":\"F shifted to 200\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":200,\"high\":210},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}")
CLASH_ID=$(echo "$CLASH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$CLASH_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("shifted to 200 (inside forbidden): has_conflicts =", d["has_conflicts"])'

echo "== 25. the search is non-mutating and validates its inputs =="
post "/api/applications/$F_ID/available-ranges" \
  '{"candidate_low_min":430,"candidate_low_max":180}' | J || true
post "/api/applications/999999/available-ranges" \
  '{"candidate_low_min":180,"candidate_low_max":430}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"])'

echo "== 26. temporary outage: suspend device D (expected revision 7 -> revision 8) =="
echo "    D leaves the effective set immediately; its approval event is never rewritten."
post "/api/applications/$D_ID/suspend" \
  '{"expected_revision":7,"reason":"device powered off for repair"}' | J
curl -s "$BASE/api/applications?status=suspended" | python3 -c 'import json,sys; print("suspended ids:", [a["id"] for a in json.load(sys.stdin)["applications"]])'

echo "== 27. illegal/duplicate suspend operations are refused (no revision consumed) =="
echo "    -- suspend D twice (already suspended):"
post "/api/applications/$D_ID/suspend" '{"expected_revision":8}' | J || true
echo "    -- approve a suspended grant (use /resume instead):"
post "/api/applications/$D_ID/approve" '{"expected_revision":8}' | J || true
echo "    -- suspend a pending application:"
post "/api/applications/$F_ID/suspend" '{"expected_revision":8}' | J || true
echo "    -- suspend an unknown id:"
post "/api/applications/999999/suspend" '{"expected_revision":8}' | J || true
echo "    -- stale expected revision on the still-active C (state is legal, revision 0 is old):"
post "/api/applications/$C_ID/suspend" '{"expected_revision":0}' | J || true

echo "== 28. while D is suspended, a device on D's own band [300,310] can be approved =="
I=$(mk "Device I" 300 310); I_ID=$(echo "$I" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$I_ID/approve" '{"expected_revision":8}' | J

echo "== 29. resuming D is re-adjudicated: clashes with I -> stays suspended, revision 9 unchanged =="
post "/api/applications/$D_ID/resume" '{"expected_revision":9}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "| suspension revisions:", [a["revision"] for a in d["suspensions"]], "| resumptions:", d["resumptions"])'

echo "== 30. a stale resume returns the current revision and keeps the grant suspended =="
post "/api/applications/$D_ID/resume" '{"expected_revision":7}' | J || true

echo "== 31. revoke I permanently (revision 10), then resume D at revision 10 -> revision 11 =="
post "/api/applications/$I_ID/revoke" '{"reason":"event cancelled"}' | J
post "/api/applications/$D_ID/resume" \
  '{"expected_revision":10,"reason":"repair finished"}' | J
echo "    D is effective again:"
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective ids:", d["effective_application_ids"])'

echo "== 32. suspend D once more, then revoke it WHILE suspended: revocation is permanent =="
post "/api/applications/$D_ID/suspend" \
  '{"expected_revision":11,"reason":"outage again"}' | J
post "/api/applications/$D_ID/revoke" '{"reason":"decommissioned"}' | J
echo "    -- resuming or suspending the revoked grant is now refused:"
post "/api/applications/$D_ID/resume" '{"expected_revision":13}' | J || true
post "/api/applications/$D_ID/suspend" '{"expected_revision":13}' | J || true

echo "== 33. history restores suspend/resume/revoke records and each revision's effective set =="
curl -s "$BASE/api/revision/8" | D_ID="$D_ID" python3 -c 'import json,sys,os; d=json.load(sys.stdin); did=int(os.environ["D_ID"]); print("rev 8: effective", d["effective_application_ids"], "| D status_as_of:", [a["status_as_of"] for a in d["approvals"] if a["application_id"]==did])'
curl -s "$BASE/api/revision/11" | D_ID="$D_ID" python3 -c 'import json,sys,os; d=json.load(sys.stdin); did=int(os.environ["D_ID"]); print("rev 11: effective", d["effective_application_ids"], "| D status_as_of:", [a["status_as_of"] for a in d["approvals"] if a["application_id"]==did])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current", d["current_revision"], "| suspensions:", [(e["revision"], e["application_id"]) for e in d["suspensions"]], "| resumptions:", [(e["revision"], e["application_id"]) for e in d["resumptions"]], "| revocations:", [(e["revision"], e["application_id"]) for e in d["revocations"]])'
