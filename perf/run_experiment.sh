#!/usr/bin/env bash
# Run one optimization experiment end to end and print a verdict.
#
#   perf/run_experiment.sh <name> [--writes]
#
# Re-runs Tier 1 (and optionally Tier 1W), diffs against the committed
# baseline, and exits non-zero if the availability invariant is violated or
# query counts regressed. Read scenarios do not mutate state and Tier 1W rolls
# every operation back, so no snapshot restore is needed between experiments --
# but dataset drift is checked anyway, because a silently changed dataset would
# invalidate the comparison.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
NAME="${1:?usage: run_experiment.sh <name> [--writes]}"
WRITES="${2:-}"

# --- provenance ------------------------------------------------------------
# Record exactly which tree this measurement describes. A stray edit -- from a
# concurrent process, an agent, or a forgotten experiment -- otherwise produces
# a number that looks authoritative and is not attributable to anything.
echo "== tree under test =="
if git diff --quiet HEAD -- nautobot/; then
  echo "  nautobot/: clean (== $(git rev-parse --short HEAD))"
else
  git diff --stat HEAD -- nautobot/ | sed 's/^/  /'
fi

# --- dataset drift check ---------------------------------------------------
EXPECTED="231 743 272 234"
ACTUAL=$(cat <<'PY' | perf/dc.sh exec -T nautobot nautobot-server shell 2>/dev/null | tail -1
from nautobot.dcim.models import Device, Interface, Cable
from nautobot.ipam.models import IPAddress
print(Device.objects.count(), Interface.objects.count(), Cable.objects.count(), IPAddress.objects.count())
PY
)
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "!! DATASET DRIFT: expected [$EXPECTED] got [$ACTUAL]"
  echo "   restore with: perf/restore_snapshot.sh   (comparison is invalid until then)"
  exit 3
fi

echo "== Tier 1 (read path) =="
perf/dc.sh exec -T nautobot python /source/perf/tier1_queries.py \
  --out "/source/perf/results/tier1-${NAME}.json" 2>&1 | tail -1

if [ "$WRITES" = "--writes" ]; then
  echo "== Tier 1W (write path) =="
  perf/dc.sh exec -T nautobot python /source/perf/tier1w_writes.py \
    --out "/source/perf/results/tier1w-${NAME}.json" 2>&1 | grep -vE "^\s|^\d{2}:" | tail -15
fi

echo "== verdict =="
python3 perf/compare.py \
  --baseline perf/baselines/tier1-baseline.json \
  --current  "perf/results/tier1-${NAME}.json" \
  --json-out "perf/results/diff-${NAME}.json"
