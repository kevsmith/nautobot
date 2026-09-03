#!/usr/bin/env bash
# Serialize wall-clock measurement, and record what tree produced the number.
#
#   perf/measure.sh <command> [args...]
#
# Runs ON the measurement host. Contention is a concurrency problem, so no
# amount of hardware fixes it -- exactly one timing run may be in flight, and
# every other agent waits or is turned away.
#
#   PERF_LOCK_WAIT  seconds to wait for the lock (default 0 = fail fast)
#   PERF_SKIP_QUIESCE=1  run anyway on a busy box (the number is then indicative
#                        only, and this script says so in its output)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ $# -ge 1 ] || { echo "usage: perf/measure.sh <command> [args...]" >&2; exit 2; }

# Outside the repo, so a sync with --delete cannot remove the lock mid-run.
LOCK=/tmp/nautobot-perf-measure.lock
HOLDER=/tmp/nautobot-perf-measure.holder
WAIT="${PERF_LOCK_WAIT:-0}"

exec 9>"$LOCK"
if ! flock -w "$WAIT" 9; then
  echo "!! a measurement is already running -- not starting a second one"
  echo "   holder: $(cat "$HOLDER" 2>/dev/null || echo unknown)"
  echo "   wait for it with PERF_LOCK_WAIT=<seconds>"
  exit 4
fi
printf 'pid=%s started=%s cmd=%s\n' "$$" "$(date -u +%FT%TZ)" "$*" > "$HOLDER"
# shellcheck disable=SC2064
trap "rm -f '$HOLDER'" EXIT

if [ "${PERF_SKIP_QUIESCE:-0}" = "1" ]; then
  echo "!! PERF_SKIP_QUIESCE=1 -- host not verified quiet; treat any timing as indicative"
else
  perf/quiesce.sh --check --wait "${PERF_SETTLE:-120}" || {
    echo "!! host is not quiet; refusing to time. Fix it, or re-run with"
    echo "   PERF_SKIP_QUIESCE=1 if you only need the deterministic counters."
    exit 3
  }
fi

# Provenance in the same breath as the number, so a figure is never orphaned
# from the tree that produced it.
echo "== tree under test =="
if [ -f perf/.provenance.json ]; then sed 's/^/  /' perf/.provenance.json; fi
if git rev-parse --git-dir >/dev/null 2>&1; then
  if git diff --quiet HEAD -- nautobot/; then
    echo "  nautobot/: clean (== $(git rev-parse --short HEAD))"
  else
    git diff --stat HEAD -- nautobot/ | sed 's/^/  /'
  fi
fi

echo "== measuring: $* =="
START=$(date -u +%FT%TZ)
"$@"
rc=$?
echo "== done (started $START, rc=$rc) =="
exit $rc
