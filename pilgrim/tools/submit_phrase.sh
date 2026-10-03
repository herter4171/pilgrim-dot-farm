#!/usr/bin/env bash
# submit_phrase.sh — submit an on-air admin message (operator phrase).
#
# Usage:
#   submit_phrase.sh "This is your administrator. <message>"
#
# The message is the copy, verbatim (RADIO.md §11 operator_phrase). The
# mandatory TTS cleanup runs server-side; this script shows any change before
# submitting. Flow: validate -> submit (202) -> poll the job until it airs.
#
# Exit 0: message aired, or still on the schedule (job id printed).
# Exit 1: rejected / failed / expired / station unreachable.
# Exit 2: usage error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"   # repo root (script lives in pilgrim/tools/)
STATION_URL="${PILGRIM_STATION_URL:-http://127.0.0.1:5000}"
POLL_S=5
MAX_POLLS=180        # 15 min; a placed phrase airs well within this (tail rule)

die() { echo "error: $*" >&2; exit 1; }
[ "$#" -eq 1 ] || { echo "usage: $0 \"admin message\"" >&2; exit 2; }
TEXT="$1"
[ -n "$TEXT" ] || die "empty message"

# Operator bearer token (AGENTS rule 7: never in the script or repo files).
TOKEN_FILE="$ROOT/.env"
[ -f "$TOKEN_FILE" ] || die ".env not found at $TOKEN_FILE"
TOKEN="$(grep '^PILGRIM_DJ_TOKEN=' "$TOKEN_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
[ -n "$TOKEN" ] || die "PILGRIM_DJ_TOKEN not set in $TOKEN_FILE"

# --------------------------------------------------------------------------- #
BODY="" CODE=""
api() { # api <path> [json-data] -> sets BODY (response) and CODE (http status)
  local tmp code
  tmp="$(mktemp)"
  if [ "$#" -eq 2 ]; then
    code="$(curl -sS -m 20 -o "$tmp" -w '%{http_code}' \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d "$2" "$STATION_URL$1")" || { rm -f "$tmp"; die "request to $STATION_URL$1 failed (station down?)"; }
  else
    code="$(curl -sS -m 20 -o "$tmp" -w '%{http_code}' \
      -H "Authorization: Bearer $TOKEN" "$STATION_URL$1")" || { rm -f "$tmp"; die "request to $STATION_URL$1 failed (station down?)"; }
  fi
  BODY="$(cat "$tmp")"; rm -f "$tmp"; CODE="$code"
}

jget() { # jget <key> -> value from BODY (empty if missing / not JSON)
  printf '%s' "$BODY" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin).get(sys.argv[1], "")
except Exception:
    v = ""
print(v if isinstance(v, (str, int, float)) else json.dumps(v))' "$1"
}

fail_http() { # fail_http <what>
  echo "error: $1 (HTTP $CODE): $BODY" >&2
  exit 1
}

# --- 1. character (from config, not hardcoded) --------------------------------
api "/api/admin/dj/character"
[ "$CODE" = "200" ] || fail_http "get character"
CHAR_ID="$(jget character_id)"
VOICE="$(jget voice)"
echo "character: $CHAR_ID (voice: $VOICE)"

# --- 2. validate (pure: no synthesis, no scheduling) --------------------------
VJ="$(TEXT="$TEXT" CHAR_ID="$CHAR_ID" python3 -c \
  'import json, os; print(json.dumps({"character_id": os.environ["CHAR_ID"], "text": os.environ["TEXT"]}))')"
api "/api/admin/dj/phrases/validate" "$VJ"
[ "$CODE" = "200" ] || fail_http "validate"
VOK="$(jget ok)"
if [ "$VOK" != "True" ]; then
  die "message rejected: $(jget rejected_reason)"
fi
CLEANED="$(jget cleaned_text)"
if [ "$CLEANED" != "$TEXT" ]; then
  echo "TTS cleanup changed the text (the CLEANED version is what airs):"
  echo "  was:     $TEXT"
  echo "  cleaned: $CLEANED"
fi
HASH="$(jget text_hash)"

# --- 3. submit -----------------------------------------------------------------
CMD_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
SJ="$(TEXT="$TEXT" HASH="$HASH" CHAR_ID="$CHAR_ID" CMD_ID="$CMD_ID" python3 -c \
  'import json, os
print(json.dumps({"command_id": os.environ["CMD_ID"], "character_id": os.environ["CHAR_ID"], "text": os.environ["TEXT"], "cleaned_text_hash": os.environ["HASH"]}))')"
api "/api/admin/dj/phrases" "$SJ"
if [ "$CODE" != "202" ]; then
  fail_http "submit"
fi
JOB_ID="$(jget job_id)"
echo "submitted: job $JOB_ID (command $CMD_ID)"

# --- 4. poll until it airs -------------------------------------------------------
last=""
for _i in $(seq 1 "$MAX_POLLS"); do
  sleep "$POLL_S"
  api "/api/admin/dj/phrases/$JOB_ID"
  [ "$CODE" = "200" ] || fail_http "poll job $JOB_ID"
  ST="$(jget status)"
  if [ "$ST" != "$last" ]; then
    echo "$(date -u '+%H:%M:%S') status: $ST"
    last="$ST"
  fi
  case "$ST" in
    aired)
      echo "AIREDS — your message is on the air (job $JOB_ID)."
      exit 0 ;;
    failed|expired|skipped)
      die "job $JOB_ID $ST: $(jget failure_reason)" ;;
  esac
done
echo "still on the schedule after $((MAX_POLLS * POLL_S / 60)) min (last status: $last)."
echo "re-check: curl -s -H \"Authorization: Bearer \$PILGRIM_DJ_TOKEN\" $STATION_URL/api/admin/dj/phrases/$JOB_ID"
exit 0
