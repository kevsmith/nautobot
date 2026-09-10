#!/usr/bin/env bash
# Restore the seeded database from a snapshot, returning the instance to
# exactly the state the baselines were measured against.
#
#   perf/scripts/restore_snapshot.sh [snapshot-file]
#
# Defaults to the large snapshot, which is what the committed baselines and
# perf/baselines/expected-counts.txt describe. Pass perf/snapshot.sql to load
# the round-one small dataset instead.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
SNAPSHOT="${1:-${PERF_SNAPSHOT:-perf/snapshot-large.sql}}"
[ -f "$SNAPSHOT" ] || { echo "$SNAPSHOT missing"; exit 1; }
echo "restoring $SNAPSHOT ..."
# Record what was running before stopping it. Unconditionally starting celery afterwards left
# the box failing its own measurement gate: quiesce.sh expects exactly db, nautobot and redis, so
# every restore ended with "NOT QUIET, extra: celery_beat celery_worker" and refused to time
# anything until someone noticed and stopped them.
WAS_RUNNING="$(perf/scripts/dc.sh ps --services --filter status=running 2>/dev/null | tr '\n' ' ')"
perf/scripts/dc.sh stop nautobot celery_worker celery_beat >/dev/null 2>&1
perf/scripts/dc.sh exec -T db psql -U nautobot -d postgres -q \
  -c "DROP DATABASE IF EXISTS nautobot WITH (FORCE);" -c "CREATE DATABASE nautobot OWNER nautobot;"
perf/scripts/dc.sh exec -T db psql -U nautobot -d nautobot -q < "$SNAPSHOT"
# `up -d`, not `start`: start fails on a container that does not exist yet, so
# on a freshly provisioned host the restore would succeed and then the script
# would exit 1 on its last line. `up -d` creates what is missing and starts
# what is present, which is correct in both cases.
RESTART="nautobot"
for svc in celery_worker celery_beat; do
  case " $WAS_RUNNING " in *" $svc "*) RESTART="$RESTART $svc" ;; esac
done
# shellcheck disable=SC2086
perf/scripts/dc.sh up -d $RESTART >/dev/null 2>&1
# The snapshot predates any migration written after it was taken, so a restore
# alone leaves the schema behind the code. Migrations are in scope on this branch
# now (see the taxonomy in perf/README.md), so the restore has to end with one.
# A no-op on an already-current schema.
perf/scripts/dc.sh exec -T nautobot nautobot-server migrate --no-input >/dev/null 2>&1
# A restore replaces django_session, so any Tier 2 session cookie minted earlier stops
# authenticating -- which once produced an A/B that completed cleanly having timed one of eight
# scenarios. The token survives (it is in the snapshot and has no expiry); the session does not.
perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/ensure_credentials.py 2>&1 | sed 's/^/  /'
echo "restored."
