#!/usr/bin/env bash
# Push perf/demo/ to hannah and run demo.sh there.
#
#   perf/demo/remote.sh provision
#   perf/demo/remote.sh up
#   perf/demo/remote.sh load
#   perf/demo/remote.sh compare 7
#
# Every invocation re-syncs first, so editing a file here and re-running is the
# whole edit loop -- there is no second copy to forget about. This directory is
# NOT part of what perf/scripts/sync.sh pushes (that targets the measurement
# tree), and it deliberately lands outside both demo trees, since the stock arm
# is a commit that predates perf/ entirely.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERF_HOST="${PERF_HOST:-kevsmith@hannah}"
REMOTE_DIR="${DEMO_REMOTE_DIR:-/home/kevsmith/demo}"

rsync -a --exclude "arms/" "$HERE/" "$PERF_HOST:$REMOTE_DIR/"
# shellcheck disable=SC2029  # $REMOTE_DIR expanding locally is the intent
# -tt, forced, even with no local terminal. Without a tty the remote command is
# not a session leader, so killing ssh here leaves it running there: an
# interrupted `up` on this host went on to stop the measurement stack and start
# two container sets on hannah anyway, and nothing locally said so. With -tt the
# remote side gets SIGHUP when the connection drops, so Ctrl-C means what it says.
# The cost is the "Pseudo-terminal will not be allocated" notice in scripted use,
# which is noise rather than a problem.
ssh -tt "$PERF_HOST" "$REMOTE_DIR/demo.sh $*"
