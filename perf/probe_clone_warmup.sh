#!/usr/bin/env bash
# Measure what a template clone costs the first requests that follow it.
#
#   perf/probe_clone_warmup.sh [rounds] [requests-per-arm]
#
# perf/reset_db.sh returns the database to baseline in ~1.3s by copying the
# template's files into a new database. Those files are cold in PostgreSQL's
# shared buffers, so the requests immediately after a clone may pay for warming
# them -- and the app's pooled connections were force-terminated, so the first
# request also pays a reconnect.
#
# If that penalty is real and not accounted for, every model in the write matrix
# carries it, and a per-model reset silently biases the numbers it exists to
# make trustworthy. So: how large is it, and how many requests until it is gone?
#
# Two arms per round. COLD is the first N requests after a clone. WARM is the
# next N with no clone in between -- same process, same database, buffers now
# populated. Per-position medians across rounds; position 1 of COLD against the
# WARM plateau is the answer.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ROUNDS="${1:-3}"
N="${2:-20}"
URL="${PERF_WARMUP_URL:-http://localhost:8180/api/dcim/devices/?limit=50}"
TOKEN="${PERF_TOKEN:-0123456789abcdef0123456789abcdef01234567}"
OUT="${PERF_WARMUP_OUT:-perf/results/clone-warmup.tsv}"
mkdir -p "$(dirname "$OUT")"

# One request, milliseconds. The HTTP status is checked on every single request:
# a clone that left the app unable to reach the database would otherwise show up
# as a very fast 500 and read as an improvement.
req() {
  local body
  body="$(curl -s -o /dev/null -w '%{http_code} %{time_total}' \
            -H "Authorization: Token $TOKEN" "$URL")"
  local code="${body%% *}" secs="${body##* }"
  if [ "$code" != "200" ]; then
    echo "NON-200 RESPONSE: $code -- measurement void" >&2
    exit 1
  fi
  awk -v s="$secs" 'BEGIN { printf "%.1f", s * 1000 }'
}

echo -e "round\tarm\tpos\tms" > "$OUT"
for r in $(seq 1 "$ROUNDS"); do
  perf/reset_db.sh || { echo "clone failed in round $r" >&2; exit 1; }
  for arm in COLD WARM; do
    for i in $(seq 1 "$N"); do
      printf '%s\t%s\t%s\t%s\n' "$r" "$arm" "$i" "$(req)" >> "$OUT"
    done
  done
  echo "round $r done"
done
echo "wrote $OUT"
