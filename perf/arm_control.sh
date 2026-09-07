#!/usr/bin/env bash
# Phase B arm control for the -37% validation.
#
# Only nautobot/ is swapped, which is what makes it safe for this script to live
# in perf/: perf/ and development/docker-compose.perf.yml do not exist on `next`,
# so a whole-tree checkout would delete the harness and the compose overlay the
# running container was created with. Every one of the 37 files that differ
# between the two arms is a modification -- no adds, no deletes -- so a
# path-scoped checkout is an exact swap in both directions.
#
#   armctl.sh hash            content hash of nautobot/, the proof the arms differ
#   armctl.sh arm <ref>       swap nautobot/ to <ref>, restart, wait until measurable
#   armctl.sh reset           clone nautobot_empty over the live database
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

nb_hash() {
  find nautobot -name "*.py" -not -path "*/project-static/*" -print0 \
    | LC_ALL=C sort -z | xargs -0 cat | sha256sum | cut -d" " -f1
}

case "${1:-}" in
hash)
  nb_hash
  ;;

arm)
  ref="${2:?usage: armctl.sh arm <ref>}"
  git checkout "$ref" -- nautobot/ || { echo "checkout failed" >&2; exit 1; }
  # Unstage immediately. A staged reversion left lying around is how five fixes
  # got undone once on this branch; nothing commits on this host, and this makes
  # sure nothing can.
  git reset -q
  echo "arm $ref  nautobot-hash $(nb_hash)"

  # uwsgi has no autoreloader, so without this the next request runs the code
  # that was loaded before the swap.
  perf/dc.sh restart nautobot >/dev/null 2>&1 </dev/null
  for i in $(seq 1 60); do
    s="$(perf/dc.sh ps --format '{{.Name}} {{.Status}}' 2>/dev/null </dev/null | grep -c 'nautobot-1.*healthy')"
    [ "$s" = "1" ] && break
    sleep 2
  done
  [ "$s" = "1" ] || { echo "nautobot did not become healthy" >&2; exit 1; }
  # Finding 35: three uwsgi workers each import Nautobot on two pinned cores, and
  # a measurement started inside that window reads bimodally high for its whole
  # arm. Three alternating rounds does not cancel it. Wait for the load to fall.
  # 180s was not enough. Chaining runs -- a screen, then an arm swap, then an apply --
  # leaves the box warm, and loadavg decays slowly, so the gate aborted an arm that was
  # otherwise fine and left an empty database behind. The ceiling stays where it is
  # (finding 35: a measurement started under load reads bimodally high for its whole arm);
  # only the patience changes.
  for i in $(seq 1 "${PERF_LOAD_WAIT_TRIES:-150}"); do
    load="$(cut -d' ' -f1 /proc/loadavg)"
    awk -v l="$load" 'BEGIN{exit !(l<=0.7)}' && { echo "ready (loadavg $load)"; exit 0; }
    sleep 4
  done
  echo "load did not settle after $((${PERF_LOAD_WAIT_TRIES:-150} * 4))s (loadavg $load)" >&2
  exit 1
  ;;

reset)
  perf/dc.sh exec -T db psql -U nautobot -d postgres -q \
    -c "DROP DATABASE IF EXISTS nautobot WITH (FORCE);" \
    -c "CREATE DATABASE nautobot OWNER nautobot TEMPLATE nautobot_empty;" </dev/null \
    || { echo "reset failed" >&2; exit 1; }
  rm -f "$REPO/perf/large-dc-dataset.state.jsonl"
  echo "reset to nautobot_empty"
  ;;

*)
  echo "usage: armctl.sh {hash|arm <ref>|reset}" >&2; exit 2 ;;
esac
