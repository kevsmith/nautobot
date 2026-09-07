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

"$REPO"/perf/scripts/arm_control.sh reset   || exit 1
"$REPO"/perf/scripts/arm_control.sh arm "$REF" || exit 1

# pg_stat_statements is loaded in the perf overlay for exactly this: attributing
# total database work across a whole run rather than a single request.
psql_ -d nautobot -q -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" >/dev/null 2>&1
psql_ -d nautobot -tAc "SELECT pg_stat_statements_reset() IS NOT NULL;" > /dev/null

START=$(date +%s)
NAUTOBOT_URL=http://localhost:8180 \
NAUTOBOT_TOKEN=0123456789abcdef0123456789abcdef01234567 \
  databot apply perf/large-dc-dataset.yml > "$OUT.log" 2>&1
RC=$?
END=$(date +%s)

CALLS=$(psql_ -d nautobot -tAc "SELECT coalesce(sum(calls),0) FROM pg_stat_statements;")
DBMS=$(psql_ -d nautobot -tAc "SELECT round(coalesce(sum(total_exec_time),0)::numeric,0) FROM pg_stat_statements;")
ROWS=$(psql_ -d nautobot -tAc "SELECT (SELECT count(*) FROM dcim_device)||'/'||(SELECT count(*) FROM dcim_interface)||'/'||(SELECT count(*) FROM dcim_cable);")
NBHASH=$("$REPO"/perf/scripts/arm_control.sh hash)

printf '%s\n' "label=$LABEL ref=$REF rc=$RC wall_s=$((END-START)) queries=$CALLS db_ms=$DBMS rows=$ROWS nautobot_hash=${NBHASH:0:16}" \
  | tee -a /tmp/apply-results.txt
tail -1 "$OUT.log"
