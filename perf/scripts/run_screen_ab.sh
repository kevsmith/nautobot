#!/usr/bin/env bash
# Run one screen against both arms and compare them.
#
#   perf/scripts/run_screen_ab.sh <reads|writes> <stock-ref> <branch-ref> [--rounds N]
#
# Runs ON the measurement host, under perf/scripts/measure.sh, which takes the lock and
# refuses to time anything on a busy box.
#
# The protocol this encodes is finding 39's, which existed only as a sequence of
# hand-run commands. Three parts of it are not optional:
#
#   Arms alternate. A single A-then-B pair cannot separate the change from
#   anything that drifted between the two runs.
#
#   Each arm proves its own tree. perf/scripts/arm_control.sh prints the content hash of
#   nautobot/ after every swap, and this script records it -- a git stash A/B
#   once measured identical code six times without noticing.
#
#   The tree goes back where it started, on the way out and on failure both.
#   A checkout left behind is how five fixes got silently reverted once.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

KIND="${1:?usage: run_screen_ab.sh <reads|writes> <stock-ref> <branch-ref> [--rounds N]}"
STOCK="${2:?missing stock ref}"
BRANCH="${3:?missing branch ref}"
ROUNDS=1
[ "${4:-}" = "--rounds" ] && ROUNDS="${5:?--rounds needs a count}"

# Neither screen needs a database reset between arms. Reads do not mutate, and
# the write screen rolls every operation back by default. What does need
# clearing is the Python process -- a rolled-back transaction restores the
# database and not the process caches (finding 38) -- and the container restart
# inside arm_control.sh does that for free on every swap.
case "$KIND" in
reads)  SCRIPT=screen_reads.py ;;
writes) SCRIPT=screen_writes.py ;;
*) echo "kind must be reads or writes" >&2; exit 2 ;;
esac

# What the tree was on when this started, so the trap has somewhere to put it
# back. A detached or dirty nautobot/ means an earlier run died mid-swap; that
# is a stop rather than something to paper over.
if ! git diff --quiet HEAD -- nautobot/; then
  echo "!! nautobot/ is dirty -- an earlier arm swap did not clean up." >&2
  echo "   inspect it, then: git checkout HEAD -- nautobot/" >&2
  exit 3
fi
ORIGINAL="$(git rev-parse HEAD)"
OUTDIR="$ROOT/perf/results"
mkdir -p "$OUTDIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

restore() {
  echo "== restoring nautobot/ to $ORIGINAL =="
  git checkout "$ORIGINAL" -- nautobot/ && git reset -q
  perf/scripts/dc.sh restart nautobot >/dev/null 2>&1 </dev/null
}
trap restore EXIT

run_arm() {
  local ref="$1" label="$2" out="$3"
  echo "== arm $label ($ref) =="
  local armout
  armout="$(perf/scripts/arm_control.sh arm "$ref")" || return 1
  echo "$armout"
  local hash
  hash="$(printf '%s\n' "$armout" | sed -n 's/.*nautobot-hash \([0-9a-f]*\).*/\1/p')"
  [ -n "$hash" ] || { echo "no tree hash from arm swap" >&2; return 1; }
  echo "$hash" > "${out%.json}.treehash"
  perf/scripts/dc.sh exec -T nautobot python "/source/perf/$SCRIPT" --out "/source/perf/results/$(basename "$out")" \
    || return 1
}

# The order reverses between rounds. Running stock first in every round is what this
# script's own header forbids, and it is the failure finding 26 recorded: two endpoints a
# change could not touch read +7.1% and +4.9% for the arm that always went first, and those
# readings had to be thrown away. Reversing does not remove drift, it stops drift from
# landing on the same arm every time -- so a result that moves against the ordering is the
# one worth trusting. With an odd round count the lead is still 2-1 rather than even; the
# controls are what say whether that matters on a given run.
for round in $(seq 1 "$ROUNDS"); do
  if [ $((round % 2)) -eq 1 ]; then
    order="$STOCK $BRANCH"
  else
    order="$BRANCH $STOCK"
  fi
  for ref in $order; do
    label=stock
    [ "$ref" = "$BRANCH" ] && label=branch
    run_arm "$ref" "$label r$round" "$OUTDIR/screen-$KIND-$label-$STAMP-r$round.json" || exit 1
  done
done

# Two arms that report the same tree hash are the same code, whatever the refs
# claimed. That check is the whole reason the hash is recorded per arm.
A="$(cat "$OUTDIR/screen-$KIND-stock-$STAMP-r1.treehash")"
B="$(cat "$OUTDIR/screen-$KIND-branch-$STAMP-r1.treehash")"
if [ "$A" = "$B" ]; then
  echo "!! both arms hashed to $A -- the swap did not take. Comparison discarded." >&2
  exit 4
fi
echo "== arms differ: stock $A against branch $B =="

echo "== comparing =="
python3 perf/scripts/compare_screen.py \
  --baseline "$OUTDIR/screen-$KIND-stock-$STAMP-r$ROUNDS.json" \
  --current  "$OUTDIR/screen-$KIND-branch-$STAMP-r$ROUNDS.json" \
  --json-out "$OUTDIR/screen-$KIND-ab-$STAMP.json"
