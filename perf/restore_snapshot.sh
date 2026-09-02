#!/usr/bin/env bash
# Restore the seeded database from perf/snapshot.sql, returning the instance to
# exactly the state the baselines were measured against.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f perf/snapshot.sql ] || { echo "perf/snapshot.sql missing"; exit 1; }
echo "restoring perf/snapshot.sql ..."
perf/dc.sh stop nautobot celery_worker celery_beat >/dev/null 2>&1
perf/dc.sh exec -T db psql -U nautobot -d postgres -q \
  -c "DROP DATABASE IF EXISTS nautobot WITH (FORCE);" -c "CREATE DATABASE nautobot OWNER nautobot;"
perf/dc.sh exec -T db psql -U nautobot -d nautobot -q < perf/snapshot.sql
perf/dc.sh start nautobot celery_worker celery_beat >/dev/null 2>&1
echo "restored."
