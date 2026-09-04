#!/usr/bin/env bash
# Restore the seeded database from a snapshot, returning the instance to
# exactly the state the baselines were measured against.
#
#   perf/restore_snapshot.sh [snapshot-file]
#
# Defaults to the large snapshot, which is what the committed baselines and
# perf/baselines/expected-counts.txt describe. Pass perf/snapshot.sql to load
# the round-one small dataset instead.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SNAPSHOT="${1:-${PERF_SNAPSHOT:-perf/snapshot-large.sql}}"
[ -f "$SNAPSHOT" ] || { echo "$SNAPSHOT missing"; exit 1; }
echo "restoring $SNAPSHOT ..."
perf/dc.sh stop nautobot celery_worker celery_beat >/dev/null 2>&1
perf/dc.sh exec -T db psql -U nautobot -d postgres -q \
  -c "DROP DATABASE IF EXISTS nautobot WITH (FORCE);" -c "CREATE DATABASE nautobot OWNER nautobot;"
perf/dc.sh exec -T db psql -U nautobot -d nautobot -q < "$SNAPSHOT"
# `up -d`, not `start`: start fails on a container that does not exist yet, so
# on a freshly provisioned host the restore would succeed and then the script
# would exit 1 on its last line. `up -d` creates what is missing and starts
# what is present, which is correct in both cases.
perf/dc.sh up -d nautobot celery_worker celery_beat >/dev/null 2>&1
# The snapshot predates any migration written after it was taken, so a restore
# alone leaves the schema behind the code. Migrations are in scope on this branch
# now (see the taxonomy in perf/README.md), so the restore has to end with one.
# A no-op on an already-current schema.
perf/dc.sh exec -T nautobot nautobot-server migrate --no-input >/dev/null 2>&1
echo "restored."
