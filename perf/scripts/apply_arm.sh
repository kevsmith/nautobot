#!/usr/bin/env bash
# One arm of the -37% validation: empty database -> swap tree -> timed apply.
#
#   perf/scripts/apply_arm.sh <git-ref> <label>
#
# Records wall clock AND the server-side query count. The query count is the
# deterministic half -- it does not move with machine load, so one run per arm
# settles it, where the wall-clock ratio needs alternating rounds.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REF="${1:?usage: apply_arm.sh <ref> <label>}"
LABEL="${2:?usage: apply_arm.sh <ref> <label>}"
OUT=/tmp/apply-$LABEL
cd "$REPO" || exit 1

psql_() { perf/scripts/dc.sh exec -T db psql -U nautobot "$@" </dev/null; }

# Where the apply's client runs. databot creates 9,972 objects across 68 models and
# builds every payload itself, so it is a sustained CPU load -- and the perf overlay
# commits all eight threads (nautobot 0,1,4,5; db and redis 2,6; celery 3,7), leaving
# the client to float across cores already allocated to what is being measured. That
# has already cost one figure a retraction. Set PERF_CLIENT_HOST to run it elsewhere.
#
# Measured cost of moving it off-box: +0.16ms per TCP connect over the LAN, tight
# across three alternating rounds, against ~198ms average per request here. The tail
# looked worse over the LAN for two rounds and the third dissolved it.
APPLY_URL="${PERF_APPLY_URL:-http://localhost:8180}"
APPLY_TOKEN="${PERF_APPLY_TOKEN:-0123456789abcdef0123456789abcdef01234567}"
CLIENT_HOST="${PERF_CLIENT_HOST:-}"
CLIENT_PATH="${PERF_CLIENT_PATH:-$REPO}"

# From a remote client, "localhost" names the client, not the host under test. databot
# would apply to whatever stack the client happens to run -- albert has one -- while the
# psql reads below still come from this host, so rows and queries would describe two
# different databases and the run would still print a plausible result line. Refuse it.
if [ -n "$CLIENT_HOST" ]; then
  case "$APPLY_URL" in
    *localhost*|*127.0.0.1*)
      echo "!! PERF_CLIENT_HOST=$CLIENT_HOST with PERF_APPLY_URL=$APPLY_URL" >&2
      echo "   From the client that URL is the client's own stack, not this host." >&2
      echo "   Set PERF_APPLY_URL to this host, e.g. http://$(hostname -f):8180" >&2
      exit 2 ;;
  esac
fi

"$REPO"/perf/scripts/arm_control.sh reset   || exit 1
"$REPO"/perf/scripts/arm_control.sh arm "$REF" || exit 1

# pg_stat_statements is loaded in the perf overlay for exactly this: attributing
# total database work across a whole run rather than a single request.
psql_ -d nautobot -q -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" >/dev/null 2>&1
psql_ -d nautobot -tAc "SELECT pg_stat_statements_reset() IS NOT NULL;" > /dev/null

START=$(date +%s)
if [ -n "$CLIENT_HOST" ]; then
  # -n so ssh cannot consume this script's stdin. A `dc.sh exec` without it ate the
  # remainder of a heredoc-driven run once already.
  #
  # ssh startup lands inside START..END as a fixed additive cost, tens to hundreds of
  # ms against a run measured in the thousands of seconds. It does not alternate away,
  # but it also does not differ between arms.
  ssh -n -o BatchMode=yes "$CLIENT_HOST" \
    "cd '$CLIENT_PATH' && NAUTOBOT_URL='$APPLY_URL' NAUTOBOT_TOKEN='$APPLY_TOKEN' databot apply perf/large-dc-dataset.yml" \
    > "$OUT.log" 2>&1
  RC=$?
  # 255 is ssh's own failure, not databot's. Without this the arm reports a wall clock
  # for an apply that never ran, and the row counts below are the only thing that says so.
  [ "$RC" -eq 255 ] && echo "!! ssh to $CLIENT_HOST failed -- no apply ran this arm" >&2
else
  NAUTOBOT_URL="$APPLY_URL" \
  NAUTOBOT_TOKEN="$APPLY_TOKEN" \
    databot apply perf/large-dc-dataset.yml > "$OUT.log" 2>&1
  RC=$?
fi
END=$(date +%s)

CALLS=$(psql_ -d nautobot -tAc "SELECT coalesce(sum(calls),0) FROM pg_stat_statements;")
DBMS=$(psql_ -d nautobot -tAc "SELECT round(coalesce(sum(total_exec_time),0)::numeric,0) FROM pg_stat_statements;")
ROWS=$(psql_ -d nautobot -tAc "SELECT (SELECT count(*) FROM dcim_device)||'/'||(SELECT count(*) FROM dcim_interface)||'/'||(SELECT count(*) FROM dcim_cable);")
NBHASH=$("$REPO"/perf/scripts/arm_control.sh hash)

printf '%s\n' "label=$LABEL ref=$REF client=${CLIENT_HOST:-local} rc=$RC wall_s=$((END-START)) queries=$CALLS db_ms=$DBMS rows=$ROWS nautobot_hash=${NBHASH:0:16}" \
  | tee -a /tmp/apply-results.txt
tail -1 "$OUT.log"

# An apply that created nothing here is the signature of a client that reached a
# different database, which a wall clock and a query count cannot themselves reveal.
if [ "$ROWS" = "0/0/0" ]; then
  echo "!! no rows created on this host -- the client did not apply to the database read above" >&2
  exit 3
fi
