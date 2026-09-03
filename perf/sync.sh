#!/usr/bin/env bash
# Push the working tree to the measurement host and prove the two trees match.
#
#   perf/sync.sh [--full] [--restart]
#
# The rsync exit code is a claim. The tree hash is the verification: a partial
# sync, a stale file left behind by a missing --delete, or an rsync that raced a
# local edit all produce a hash mismatch and a non-zero exit here, so no
# measurement can be taken against a tree nobody verified.
#
#   --full     also sync perf/snapshot*.sql and perf/dataset*.yaml (~220MB).
#              Excluded by default: they are large and effectively immutable.
#   --restart  restart the app container afterwards. Required when serving
#              under uwsgi, which has no autoreloader. `runserver` reloads on
#              its own -- but a reload racing a measurement is its own hazard,
#              so sync, then restart, then measure.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PERF_HOST="${PERF_HOST:-kevsmith@hannah}"
PERF_PATH="${PERF_PATH:-/home/kevsmith/repos/work/nautobot/nautobot}"

FULL=0; RESTART=0
for arg in "$@"; do
  case "$arg" in
    --full)    FULL=1 ;;
    --restart) RESTART=1 ;;
    *) echo "usage: perf/sync.sh [--full] [--restart]" >&2; exit 2 ;;
  esac
done

# .git is included deliberately, so the remote is a faithful mirror and
# run_experiment.sh's "tree under test" block reports the same thing there as
# here. Do not run git operations locally while a sync is in flight.
EXCLUDES=(
  --exclude "perf/results/"
  --exclude "__pycache__/"
  --exclude "*.pyc"
  --exclude "node_modules/"
  --exclude ".venv/"
  --exclude ".mypy_cache/"
  --exclude ".ruff_cache/"
  --exclude ".pytest_cache/"
)
if [ "$FULL" -eq 0 ]; then
  EXCLUDES+=(--exclude "perf/snapshot*.sql" --exclude "perf/dataset*.yaml")
fi

# --- provenance -----------------------------------------------------------
# Written before the sync so it travels with it. Every result file should carry
# this, so a number is always attributable to a specific tree.
COMMIT="$(git rev-parse --short HEAD)"
DIRTY="$(git status --porcelain | wc -l | tr -d ' ')"
cat > perf/.provenance.json <<PROV
{
  "commit": "$COMMIT",
  "dirty_paths": $DIRTY,
  "synced_from": "$(hostname -s)",
  "synced_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
PROV

echo "== sync $PERF_HOST:$PERF_PATH  (HEAD $COMMIT, $DIRTY dirty) =="
# -a preserves symlinks; the `docs -> nautobot/docs` link does not survive a
# sync without it, which shows up as a deletion in git status on the far side.
rsync -a --delete "${EXCLUDES[@]}" ./ "$PERF_HOST:$PERF_PATH/"

# --- verification ---------------------------------------------------------
# Same file set, same order, same digest on both sides. Relative paths only, so
# the hash does not depend on where the tree lives.
# perf/ is in the hash set deliberately: the harness scripts decide what gets
# measured and how, so a stale copy of one of them on the far side is exactly as
# invalidating as stale product code. Extensions are chosen so the large
# gitignored artifacts stay out -- snapshot*.sql and dataset*.yaml match neither
# "*.yml" nor anything else listed, and perf/results/*.json is excluded too.
HASH_CMD='find nautobot development docker perf -type f \( -name "*.py" -o -name "*.sh" -o -name "*.yml" -o -name "*.ini" -o -name "*.html" \) -print0 | LC_ALL=C sort -z | xargs -0 cat | '
if command -v sha256sum >/dev/null; then LOCAL_SUM="sha256sum"; else LOCAL_SUM="shasum -a 256"; fi
HERE="$(eval "cd '$ROOT' && $HASH_CMD $LOCAL_SUM" | awk '{print $1}')"
THERE="$(ssh -o BatchMode=yes "$PERF_HOST" "cd '$PERF_PATH' && $HASH_CMD sha256sum" | awk '{print $1}')"

if [ "$HERE" != "$THERE" ]; then
  echo "!! TREE MISMATCH after sync -- do not measure"
  echo "   here:  $HERE"
  echo "   there: $THERE"
  exit 1
fi
echo "== trees match: ${HERE:0:16} =="

if [ "$RESTART" -eq 1 ]; then
  echo "== restarting nautobot =="
  ssh -o BatchMode=yes "$PERF_HOST" "cd '$PERF_PATH' && perf/dc.sh restart nautobot" >/dev/null
  echo "   restarted."
fi
