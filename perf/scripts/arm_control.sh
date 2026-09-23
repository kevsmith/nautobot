#!/usr/bin/env bash
# Phase B arm control for the -37% validation.
#
# Only nautobot/ is swapped, which is what makes it safe for this script to live
# in perf/: perf/ and development/docker-compose.perf.yml do not exist on `next`,
# so a whole-tree checkout would delete the harness and the compose overlay the
# running container was created with.
#
# `git checkout <ref> -- nautobot/` overwrites but never deletes, so a file the
# branch adds survives into the stock arm. This once claimed that could not happen
# -- "every one of the 37 files that differ is a modification, no adds, no deletes"
# -- and finding 22 then added a migration. Arming to `next` left it in place and
# the stock arm hashed 0e1d03a4e (against next's own e308df687), so the protocol's
# content-hash proof no longer matched the tree it named. The migration happens to
# be inert during an apply, which is exactly why it went unnoticed; the next added
# file need not be.
#
# The fix for that was HEAD-relative and covered two refs only. With a third ref in
# the sequence it fails again, and the same way: arming stock -> old-branch -> stock
# left 40 integration tests upstream had deleted in the stock arm, because they are
# absent from both the target ref and HEAD. The removal set is now computed against
# the working tree, so what arms to a ref is that ref and nothing else.
#
#   armctl.sh hash            content hash of nautobot/, the proof the arms differ
#   armctl.sh arm <ref>       swap nautobot/ to <ref>, restart, wait until measurable
#   armctl.sh reset           clone nautobot_empty over the live database
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 1

# Hashes templates as well as Python. It used to be *.py only, which made the content hash --
# the thing the protocol leans on to prove two arms differ -- blind to a template-only change.
# Two arms differing only in a .html file reported the same hash, so the proof read as satisfied
# while proving nothing. No published figure was affected: findings 51 and 58 are the only
# accepted product changes touching templates and both also changed .py, so their hashes moved.
# Caught when three drawer-stub arms, differing only in object_list.html, all hashed 368a99a04.
#
# project-static stays excluded: it is build output, regenerated per image, and including it
# makes the hash depend on whether docs happen to have been built.
nb_hash() {
  find nautobot \( -name "*.py" -o -name "*.html" -o -name "*.txt" \) \
    -not -path "*/project-static/*" -print0 \
    | LC_ALL=C sort -z | xargs -0 cat | sha256sum | cut -d" " -f1
}

case "${1:-}" in
hash)
  nb_hash
  ;;

arm)
  ref="${2:?usage: armctl.sh arm <ref>}"
  # __pycache__ is excluded from the checkout, not merely from the hash. Upstream
  # tracks one file under it -- nautobot/core/celery/__pycache__/task.cpython-313.pyc.140297736092592,
  # whose name ends in digits rather than .pyc and so slips past .gitignore. A tree
  # synced with rsync does not carry it (the sync skips __pycache__, which the
  # container writes through the bind mount as root), so git calls it deleted and a
  # path-scoped checkout tries to recreate it inside a directory the container has
  # since remade as root. That fails with "Permission denied" and takes the whole arm
  # with it. Nothing under __pycache__ is in nb_hash, so leaving it alone costs the
  # arm nothing.
  #
  # On failure, unstage before exiting. `git checkout <ref> -- <path>` STAGES what it
  # writes, and the `git reset` below is far enough down that an abort never reaches
  # it -- which leaves a staged, half-applied reversion sitting in the index. That is
  # the mechanism that silently undid five fixes on this branch once.
  if ! git checkout "$ref" -- nautobot/ ':(exclude)nautobot/**/__pycache__/*'; then
    git reset -q
    echo "checkout failed; the index has been unstaged" >&2
    echo "   the working tree is part-way between arms -- restore it with:" >&2
    echo "     git checkout -- nautobot/ ':(exclude)nautobot/**/__pycache__/*'" >&2
    echo "   (the exclusion is not optional: without it this recovery hits the same" >&2
    echo "    root-owned __pycache__ that the arm above excludes, and fails too)" >&2
    exit 1
  fi
  # Remove what the target ref does not have, computed against the WORKING TREE
  # rather than against HEAD.
  #
  # This used to diff --diff-filter=A <ref>..HEAD, i.e. only the files HEAD adds
  # relative to <ref>. That covers the two-ref case and silently fails the moment a
  # third ref is involved: a file left behind by an earlier arm is in neither <ref>
  # nor HEAD, so it is outside that set, and `git checkout <ref> -- nautobot/`
  # overwrites but never deletes. Arming stock -> old-branch -> stock left 40
  # integration tests upstream had deleted sitting in the stock arm, and the same
  # ref hashed two different ways -- which voids the proof that two arms differ.
  #
  # project-static is excluded because it is build output nb_hash already ignores,
  # and __pycache__ because discarding it would put a cold import on the next run.
  ref_files="$(mktemp)"; wt_files="$(mktemp)"
  trap 'rm -f "$ref_files" "$wt_files"' RETURN 2>/dev/null || true
  git ls-tree -r --name-only "$ref" -- nautobot/ | LC_ALL=C sort > "$ref_files"
  find nautobot -type f \
    -not -path "nautobot/project-static/*" \
    -not -path "*/__pycache__/*" \
    | LC_ALL=C sort > "$wt_files"
  extra="$(comm -13 "$ref_files" "$wt_files")"
  rm -f "$ref_files" "$wt_files"
  if [ -n "$extra" ]; then
    echo "$extra" | while IFS= read -r f; do [ -n "$f" ] && rm -f "$f"; done
    find nautobot -type d -empty -delete 2>/dev/null
    n=$(echo "$extra" | grep -c .)
    if [ "$n" -le 6 ]; then
      echo "  removed $n file(s) absent from $ref: $(echo $extra | tr '\n' ' ')"
    else
      echo "  removed $n file(s) absent from $ref, including: $(echo $extra | head -4 | tr '\n' ' ')..."
    fi
  fi
  # Unstage immediately. A staged reversion left lying around is how five fixes
  # got undone once on this branch; nothing commits on this host, and this makes
  # sure nothing can.
  git reset -q
  echo "arm $ref  nautobot-hash $(nb_hash)"

  # uwsgi has no autoreloader, so without this the next request runs the code
  # that was loaded before the swap.
  perf/scripts/dc.sh restart nautobot >/dev/null 2>&1 </dev/null
  for i in $(seq 1 60); do
    s="$(perf/scripts/dc.sh ps --format '{{.Name}} {{.Status}}' 2>/dev/null </dev/null | grep -c 'nautobot-1.*healthy')"
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
  perf/scripts/dc.sh exec -T db psql -U nautobot -d postgres -q \
    -c "DROP DATABASE IF EXISTS nautobot WITH (FORCE);" \
    -c "CREATE DATABASE nautobot OWNER nautobot TEMPLATE nautobot_empty;" </dev/null \
    || { echo "reset failed" >&2; exit 1; }
  rm -f "$REPO/perf/large-dc-dataset.state.jsonl"
  echo "reset to nautobot_empty"
  ;;

*)
  echo "usage: armctl.sh {hash|arm <ref>|reset}" >&2; exit 2 ;;
esac
