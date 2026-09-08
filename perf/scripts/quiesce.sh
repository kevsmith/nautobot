#!/usr/bin/env bash
# Put the measurement host into a state where a wall-clock number means
# something, and refuse to pretend otherwise.
#
#   perf/scripts/quiesce.sh            stop everything that is not being measured
#   perf/scripts/quiesce.sh --check    report only; non-zero if not quiet
#
# Runs ON the measurement host. Round one produced four commits that could quote
# no wall-clock figure at all because the box was busy, and two "regressions"
# (2ms and 11%) that dissolved on a third round. Both are contention, not
# hardware -- so this is the part a faster machine cannot fix.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

CHECK_ONLY=0
SETTLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    # Wait up to N seconds for the load average to fall under the ceiling before
    # judging. Starting a container or restoring a snapshot leaves the 1-minute
    # average elevated for a while after the work itself is done, and the right
    # response to that is to wait rather than to raise the ceiling.
    --wait)  shift; SETTLE="${1:-120}" ;;
    *) echo "usage: perf/scripts/quiesce.sh [--check] [--wait SECONDS]" >&2; exit 2 ;;
  esac
  shift
done

MAX_LOAD="${PERF_MAX_LOAD:-1.0}"
# Services that may run during a measurement. Anything else is contention.
MEASURED="db redis nautobot"
rc=0

# --- tools, at absolute paths -----------------------------------------------
# Never resolve a measurement-relevant binary through the ambient PATH. On this
# host cassowary was invisible to non-interactive shells (brew shellenv lives
# below .bashrc's non-interactive guard) and `python3` meant two different
# interpreters depending on whether a human or an agent ran it. Both were silent
# and one would have surfaced only when Tier 2 first ran.
echo "== tools =="
for t in /usr/bin/python3 /usr/local/bin/cassowary; do
  if [ -x "$t" ]; then
    echo "  ok      $t"
  else
    echo "  MISSING $t"; rc=1
  fi
done

# --- nothing running but the measured services ------------------------------
echo "== containers =="
RUNNING="$(perf/scripts/dc.sh ps --services --filter status=running 2>/dev/null | sort | tr '\n' ' ')"
EXTRA=""
for svc in $RUNNING; do
  case " $MEASURED " in *" $svc "*) ;; *) EXTRA="$EXTRA $svc" ;; esac
done
echo "  running:$RUNNING"
if [ -n "$EXTRA" ]; then
  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "  NOT QUIET, extra:$EXTRA"; rc=1
  else
    echo "  stopping:$EXTRA"
    # celery_beat fires scheduled work that lands mid-measurement; ui_build is
    # an npm watcher that rebuilds when a sync touches the tree.
    # shellcheck disable=SC2086
    perf/scripts/dc.sh stop $EXTRA >/dev/null 2>&1
    echo "  stopped."
  fi
fi
for svc in $MEASURED; do
  case " $RUNNING " in
    *" $svc "*) ;;
    *) echo "  MISSING  $svc is not running"; rc=1 ;;
  esac
done

# --- the box itself ---------------------------------------------------------
echo "== host =="
read -r L1 _ < /proc/loadavg
waited=0
while awk -v l="$L1" -v m="$MAX_LOAD" 'BEGIN{exit !(l>m)}' && [ "$waited" -lt "$SETTLE" ]; do
  sleep 10; waited=$((waited + 10))
  read -r L1 _ < /proc/loadavg
  echo "  settling ${waited}s: loadavg(1m) $L1"
done
echo "  loadavg(1m) $L1  (ceiling $MAX_LOAD)"
if awk -v l="$L1" -v m="$MAX_LOAD" 'BEGIN{exit !(l>m)}'; then
  echo "  TOO BUSY -- wall clock from this host would be noise"
  ps -eo pcpu,comm --sort=-pcpu | sed -n '2,5p' | sed 's/^/    /'
  rc=1
fi

# Clock policy is a gate, not a warning. A governor that scales, or turbo left
# on, makes every wall-clock number from this box drift run to run -- which is
# the same class of defect as a busy box above, and was treated as a remark.
# It stayed a remark until a distro upgrade reset the governor to `powersave`
# and the only symptom was one WARN line above a cheerful "== QUIET ==".
#
# The expectations are overridable because they describe this host: an Intel
# i5-8259U on intel_pstate with turbo disabled in firmware. Set either variable
# to the empty string to drop that check on a rig where it does not apply.
EXPECT_GOV="${PERF_EXPECT_GOVERNOR-performance}"
EXPECT_TURBO="${PERF_EXPECT_NO_TURBO-1}"

# Every policy, not just cpu0. The governor is per-policy, so a boot-time loop
# that died halfway leaves cpu0 correct and the remaining cores scaling.
GOV="$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u | tr '\n' ' ')"
GOV="${GOV% }"
TURBO="$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo unknown)"
echo "  governor ${GOV:-unknown} / no_turbo $TURBO"
if [ -n "$EXPECT_GOV" ] && [ "$GOV" != "$EXPECT_GOV" ]; then
  echo "  WRONG GOVERNOR -- expected '$EXPECT_GOV' on every CPU, got '${GOV:-unknown}'"
  echo "                   clocks will vary run to run; wall clock from this host is void"
  rc=1
fi
if [ -n "$EXPECT_TURBO" ] && [ "$TURBO" != "$EXPECT_TURBO" ]; then
  echo "  WRONG TURBO STATE -- expected no_turbo=$EXPECT_TURBO, got $TURBO"
  rc=1
fi

if [ "$rc" -eq 0 ]; then echo "== QUIET =="; else echo "== NOT QUIET (rc=$rc) =="; fi
exit $rc
