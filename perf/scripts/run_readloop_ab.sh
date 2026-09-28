#!/usr/bin/env bash
# Drive the 57-scenario read loop against both arms, over alternating rounds.
#
#   perf/scripts/run_readloop_ab.sh <stock-ref> <branch-ref> [--prefix P]
#                                   [--wall-rounds N] [--det-rounds M]
#
# Runs ON the measurement host. The read loop had no driver: run_screen_ab.sh
# covers the two screens and the loop was hand-run, which is how the 2026-09-22
# refresh produced its rb-* artifacts. Hand-running it again would repeat a
# twenty-step sequence by eye, so the sequence is written down here instead.
#
# The protocol is run_screen_ab.sh's, and the same three parts are not optional:
#
#   Arms alternate, and the ORDER reverses on even rounds. A single A-then-B
#   pair cannot separate the change from anything that drifted between the runs.
#
#   Each arm proves its own tree. arm_control.sh prints the content hash of
#   nautobot/ after every swap and this script records one .treehash per arm per
#   round, then refuses to finish if the two arms ever hashed the same -- a git
#   stash A/B once measured identical code six times without noticing.
#
#   The tree goes back where it started, on the way out and on failure both. A
#   checkout left behind is how five fixes got silently reverted once.
#
# Two round counts, because the instruments are not alike:
#
#   --det-rounds   tier1 (query counts) and tier1w (write path). Deterministic
#                  and machine-independent; these have reproduced bit-identical
#                  across CPU architectures. Two rounds is a reproducibility
#                  check, not a noise average.
#   --wall-rounds  tier2 (over HTTP) and bench_endpoints (in-process). Wall
#                  clock needs five: a 2ms and an 11% "regression" on this
#                  branch both dissolved on a third round, and the row this
#                  replaces was itself a median of five.
#
# Tier 2 reuses the URL set each arm dumped in ITS round 1. The set resolves
# concrete primary keys, so re-dumping per round would let a 404 shrink coverage
# mid-campaign and silently change what the median is over.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

STOCK="${1:?usage: run_readloop_ab.sh <stock-ref> <branch-ref> [--prefix P] [--wall-rounds N] [--det-rounds M]}"
BRANCH="${2:?missing branch ref}"
shift 2
PREFIX="rl"
WALL_ROUNDS=5
DET_ROUNDS=2
TIER2_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)      shift; PREFIX="${1:?--prefix needs a value}" ;;
    --wall-rounds) shift; WALL_ROUNDS="${1:?--wall-rounds needs a count}" ;;
    --det-rounds)  shift; DET_ROUNDS="${1:?--det-rounds needs a count}" ;;
    # Re-run only the over-HTTP leg, reusing the URL sets a previous run of the
    # same prefix dumped. For when tier2 is void and the in-process instruments
    # are not, which is exactly how this flag came to exist.
    --tier2-only)  TIER2_ONLY=1; DET_ROUNDS=0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

RES="perf/results"
mkdir -p "$RES"

# Tier 2 goes over HTTP and therefore needs credentials. Without them every UI
# view answers 302 to /login/ and tier2_latency.py correctly refuses to time a
# redirect -- so the run completes, writes a file, and reports one endpoint out
# of 57. That is what happened on the first run of this script: 56 of 57 skipped
# with "probe status 302", five rounds on both arms, all void. The instrument
# warned; the driver piped it through `tail -3` and swallowed the warning.
#
# The session key is the fixed one ensure_credentials.py mints, so it can be
# named here rather than scraped out of a restore's output.
export NAUTOBOT_TOKEN="${NAUTOBOT_TOKEN:-0123456789abcdef0123456789abcdef01234567}"
export NAUTOBOT_SESSIONID="${NAUTOBOT_SESSIONID:-perfbotperfbotperfbotperfbot0000}"
BASE_URL="${PERF_BASE_URL:-http://localhost:8180}"

# Prove the cookie before spending hours on runs that cannot use it. A session
# lives in django_session, so a restore or an `arm_control.sh reset` since it was
# minted has already invalidated it.
probe="$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
  -H "Cookie: sessionid=$NAUTOBOT_SESSIONID" "$BASE_URL/dcim/devices/?per_page=1")"
if [ "$probe" != "200" ]; then
  echo "!! session cookie returns $probe on a UI list, not 200." >&2
  echo "   Tier 2 would skip every UI view and report a run that timed almost nothing." >&2
  echo "   Re-mint with: perf/scripts/ensure_credentials.py (restore_snapshot.sh calls it)" >&2
  exit 1
fi
echo "== session cookie verified (200 on a UI list) =="

# What the tree was on when this started, so the trap has somewhere to put it
# back. A dirty nautobot/ means an earlier run died mid-swap; that is a stop
# rather than something to paper over.
if ! git diff --quiet HEAD -- nautobot/; then
  echo "!! nautobot/ is dirty -- an earlier arm was left behind. Restore it first:" >&2
  echo "     git checkout -- nautobot/ ':(exclude)nautobot/**/__pycache__/*'" >&2
  exit 1
fi
START_REF="$(git rev-parse HEAD)"

restore_tree() {
  echo "== restoring tree to ${START_REF:0:9} =="
  bash perf/scripts/arm_control.sh arm "$START_REF" >/dev/null 2>&1 \
    || echo "!! could not restore the tree -- do it by hand before measuring anything" >&2
}
trap restore_tree EXIT INT TERM

ref_for() { [ "$1" = "stock" ] && echo "$STOCK" || echo "$BRANCH"; }

echo "== read loop A/B =="
echo "   stock        $STOCK  ($(git rev-parse --short "$STOCK"))"
echo "   branch       $BRANCH  ($(git rev-parse --short "$BRANCH"))"
echo "   prefix       $PREFIX"
echo "   wall rounds  $WALL_ROUNDS   det rounds $DET_ROUNDS"
echo "   started      $(date -u +%Y-%m-%dT%H:%M:%SZ)"

for r in $(seq 1 "$WALL_ROUNDS"); do
  # Reverse the order on even rounds. Arm reversal cancels drift; it does not
  # cancel a deterministic per-arm bias, which is what the medians are for.
  if [ $((r % 2)) -eq 1 ]; then ORDER="stock branch"; else ORDER="branch stock"; fi
  echo
  echo "== round $r/$WALL_ROUNDS  (order: $ORDER) =="

  for arm in $ORDER; do
    ref="$(ref_for "$arm")"
    echo "-- arm $arm -> $ref"
    if ! bash perf/scripts/arm_control.sh arm "$ref"; then
      echo "!! arm to $ref failed in round $r; stopping" >&2
      exit 1
    fi
    hash="$(bash perf/scripts/arm_control.sh hash)"
    echo "$hash" > "$RES/$PREFIX-$arm-r$r.treehash"
    echo "   tree $hash"

    if [ "$r" -le "$DET_ROUNDS" ]; then
      echo "   tier1 (query counts)"
      bash perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/tier1_queries.py \
        --out "/source/$RES/$PREFIX-tier1-$arm-r$r.json" \
        --dump-urls "/source/$RES/$PREFIX-urls-$arm-r$r.json" 2>&1 | tail -3
      echo "   tier1w (write path)"
      bash perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/tier1w_writes.py \
        --out "/source/$RES/$PREFIX-tier1w-$arm-r$r.json" 2>&1 | tail -3
    fi

    # Wall clock, behind the lock. measure.sh re-checks quiesce and refuses on a
    # busy box; that refusal is the point, so it is not worked around here.
    if [ "$TIER2_ONLY" -eq 0 ]; then
      echo "   bench (in-process)"
      bash perf/scripts/measure.sh bash perf/scripts/dc.sh exec -T nautobot \
        python /source/perf/scripts/bench_endpoints.py --reps 15 \
        --out "/source/$RES/$PREFIX-bench-$arm-r$r.json" 2>&1 | tail -3
    fi

    urls="$RES/$PREFIX-urls-$arm-r1.json"
    if [ ! -f "$urls" ]; then
      echo "!! $urls missing -- round 1 must run with --det-rounds >= 1" >&2
      exit 1
    fi
    out="$RES/$PREFIX-tier2-$arm-r$r.json"
    echo "   tier2 (over HTTP, -c 1)"
    # Not piped through `tail` any more: this instrument warns on a partial skip
    # rather than failing, and truncating its output is how that warning was lost.
    bash perf/scripts/measure.sh /usr/bin/python3 perf/scripts/tier2_latency.py \
      --urls "$urls" --out "$out" -c 1 2>&1 | grep -vE '^\s*$'

    # Coverage guard. A skip is a warning inside tier2_latency.py, deliberately --
    # skipping UI views for want of a session is not the same as crashing. From a
    # driver's side it is fatal, because the median is then over a different set
    # than the one the row claims. Compare against the URL set actually asked for.
    timed_expected="$(/usr/bin/python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$urls")"
    timed_got="$(/usr/bin/python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(sum(1 for e in d['endpoints'] if e.get('server_ms_median') and not e.get('skipped')))" "$out")"
    echo "   tier2 timed $timed_got of $timed_expected"
    if [ "$timed_got" -lt "$timed_expected" ]; then
      echo "!! tier2 timed $timed_got of $timed_expected endpoints in round $r ($arm)." >&2
      /usr/bin/python3 -c "
import json,sys,collections
d=json.load(open(sys.argv[1]))
c=collections.Counter(str(e.get('skipped')) for e in d['endpoints'] if e.get('skipped'))
for reason,n in c.most_common(5): print(f'   {n:>4}  {reason[:100]}', file=sys.stderr)" "$out"
      echo "   Refusing to continue: the medians would describe a shrunken set." >&2
      exit 1
    fi
  done
done

# The toggle proof, applied to every round rather than to one pair. Equal hashes
# mean the two arms ran the same code and every number above is void.
echo
echo "== arm hashes =="
void=0
for r in $(seq 1 "$WALL_ROUNDS"); do
  s="$(cat "$RES/$PREFIX-stock-r$r.treehash" 2>/dev/null)"
  b="$(cat "$RES/$PREFIX-branch-r$r.treehash" 2>/dev/null)"
  if [ -z "$s" ] || [ -z "$b" ]; then
    echo "   r$r  MISSING a hash -- round did not complete"; void=1
  elif [ "$s" = "$b" ]; then
    echo "   r$r  IDENTICAL $s -- the arms did not differ; this round is VOID"; void=1
  else
    echo "   r$r  stock ${s:0:12}  branch ${b:0:12}  distinct"
  fi
done
echo "   finished $(date -u +%Y-%m-%dT%H:%M:%SZ)"
[ "$void" -eq 0 ] || { echo "!! at least one round is void -- do not publish these numbers" >&2; exit 1; }
echo "== read loop A/B complete =="
