#!/usr/bin/env bash
# Return the database to baseline state in about a second, by cloning a
# pristine template rather than replaying the snapshot.
#
#   perf/reset_db.sh --build    (re)build the template from the current database
#   perf/reset_db.sh            clone the template over the live database
#
# Why this exists. perf/restore_snapshot.sh takes ~49 seconds, and four of its
# five phases are things a measurement loop does not need every iteration:
#
#     container stop        5.9s   not needed -- DROP ... WITH (FORCE) evicts
#     drop/create           0.5s   needed
#     psql replay          19.1s   not needed -- CREATE ... TEMPLATE copies files
#     container up         12.8s   not needed
#     migrate (no-op)       9.6s   not needed -- the template is already migrated
#
# What is left is a 1-second file copy. That is what makes per-operation
# isolation affordable for the write matrix, and it is what re-prices finding 29:
# restore-based isolation was declined at ~70s per arm, which is not what it
# costs.
#
# restore_snapshot.sh remains the authority and the slow path. It is what you run
# to establish the state this script then copies, and what you run when the
# template is stale or missing.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TEMPLATE="${PERF_TEMPLATE_DB:-nautobot_pristine}"
LIVE="${PERF_LIVE_DB:-nautobot}"

psql_postgres() { perf/dc.sh exec -T db psql -U nautobot -d postgres -q "$@"; }

# Fingerprint the migration files in the tree. The template is a database at a
# fixed schema; if the tree's migrations have changed since it was built, a clone
# silently hands back a schema that is behind the code. restore_snapshot.sh
# defends against that by always running `migrate`, which is 9.6 of its 49
# seconds. This is the same guard for free: pure filesystem, no Django startup.
# `sha256sum` on the Linux measurement host, `shasum -a 256` on a Mac. Resolved
# once here rather than assumed, because this script has to give the same answer
# on both or the guard fires spuriously.
if command -v sha256sum >/dev/null 2>&1; then
  _sha() { sha256sum | cut -d" " -f1; }
else
  _sha() { shasum -a 256 | cut -d" " -f1; }
fi

# Hashes names *and* contents. Names alone would miss a migration edited in
# place, which is the case most likely to leave the template silently wrong.
migration_fingerprint() {
  local list
  list="$(find nautobot -path "*/migrations/*.py" ! -name "__init__.py" -print | LC_ALL=C sort)"
  { printf '%s\n' "$list"; printf '%s\n' "$list" | xargs cat; } | _sha
}

usage() { echo "usage: perf/reset_db.sh [--build]" >&2; exit 2; }

BUILD=0
case "${1:-}" in
  --build) BUILD=1 ;;
  "")      ;;
  *)       usage ;;
esac

FP="$(migration_fingerprint)"

if [ "$BUILD" = 1 ]; then
  # Build from whatever is live, so the caller decides what "baseline" means --
  # normally the state left by restore_snapshot.sh. The fingerprint is recorded
  # as a comment on the template database rather than a table, so the clones stay
  # free of anything this harness added. Database comments are not copied by
  # CREATE DATABASE ... TEMPLATE, which is exactly the behaviour wanted here.
  echo "building template $TEMPLATE from $LIVE ..."
  psql_postgres -c "DROP DATABASE IF EXISTS $TEMPLATE WITH (FORCE);" \
    || { echo "FAILED to drop existing template" >&2; exit 1; }
  psql_postgres -c "CREATE DATABASE $TEMPLATE OWNER nautobot TEMPLATE $LIVE;" \
    || { echo "FAILED to create template -- is something connected to $LIVE?" >&2; exit 1; }
  psql_postgres -c "COMMENT ON DATABASE $TEMPLATE IS 'perf-migrations:$FP';" \
    || { echo "FAILED to record migration fingerprint" >&2; exit 1; }
  echo "built. migrations fingerprint ${FP:0:12}"
  exit 0
fi

# --- clone path --------------------------------------------------------------
STORED="$(psql_postgres -tAc \
  "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = '$TEMPLATE';" \
  | tr -d '[:space:]')"

if [ -z "$STORED" ]; then
  echo "no template '$TEMPLATE' (or it carries no fingerprint)." >&2
  echo "run: perf/restore_snapshot.sh && perf/reset_db.sh --build" >&2
  exit 1
fi

if [ "$STORED" != "perf-migrations:$FP" ]; then
  # Refuse rather than fall back to the slow path. A reset that sometimes takes
  # 1s and sometimes 49s would put a 48-second spike inside a measurement loop
  # at a moment nobody chose.
  echo "STALE TEMPLATE -- migrations in the tree do not match the template." >&2
  echo "  template: $STORED" >&2
  echo "  tree:     perf-migrations:$FP" >&2
  echo "run: perf/restore_snapshot.sh && perf/reset_db.sh --build" >&2
  exit 1
fi

psql_postgres -c "DROP DATABASE IF EXISTS $LIVE WITH (FORCE);" \
              -c "CREATE DATABASE $LIVE OWNER nautobot TEMPLATE $TEMPLATE;" \
  || { echo "FAILED to clone $TEMPLATE -> $LIVE" >&2; exit 1; }
