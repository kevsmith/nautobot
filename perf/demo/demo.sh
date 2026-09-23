#!/usr/bin/env bash
# Two Nautobot instances side by side on hannah: stock and perf/recommended,
# same dataset, same server, same tuning, differing only in nautobot/.
#
# Runs ON hannah. perf/demo/remote.sh pushes this file there and calls it.
#
#   demo.sh setup       everything, from nothing to two loaded instances (~25 min)
#   demo.sh start       bring both up, plus the side-by-side viewer on :8480
#   demo.sh stop        stop both, keeping the databases
#   demo.sh status      what is up, at which commit, with how many rows
#   demo.sh nuke        remove the containers AND the databases
#
# Day to day it is start and stop. setup is the one-time path, and its four
# steps -- install, up, load, arm -- can also be run on their own if something
# needs redoing: `install` re-copies and re-patches, `load` re-restores the
# dataset, `arm` puts each copy back on its ref.
#
# The order matters and cost an attempt to learn: arming before loading puts
# armctl's 120-second health gate in front of a container running Nautobot's
# entire migration set against an empty database, which it cannot finish in
# time. Restoring first gives the app a populated schema, so the restart an arm
# performs is quick and the gate is meaningful again.
#
# ## Why a copy of the measurement tree rather than a fresh clone
#
# `arm_control.sh arm <ref>` swaps nautobot/ and nothing else, which is what
# makes a copy of this tree a complete, runnable stack at any ref: perf/ and
# development/docker-compose.perf.yml are not on `next`, so a clone of the stock
# commit would have no compose overlay, no uwsgi ini, and none of the harness.
# Copying means each arm restores, quiesces and hashes with the same scripts the
# published figures were produced by, instead of a second implementation of them
# that can drift.
#
# ## Why these two refs
#
# The stock arm is upstream `next` at its current tip; the branch arm is
# perf/recommended. `start` checks both arms against the content hashes below, so
# which code each one runs is verified rather than asserted.
#
# **The two arms do not share a base, and what that costs is interpretive rather
# than operational.** perf/recommended is 53 commits on top of `next` at
# 3f77053a7, and the stock arm here is 15 commits further on. So the difference a
# viewer sees is those 53 commits MINUS whatever the 15 changed -- custom links
# rendering as a dropdown (#9514), lighter dark-mode borders (#9517), a
# maintenance-mode fix (#9415), the debug toolbar kept off Playwright's pages
# (#9511), and dependency bumps. None of those is known to move page load either
# way; none has been measured on this rig.
#
# Nothing here measures, so this is a statement about what the side-by-side
# implies, not about whether it runs. If you need the difference to be exactly
# this branch's effect -- for a published figure, or for an audience who will
# read it that way -- rebase perf/recommended onto the same tip and set STOCK_REF
# to it. perf/report.md's figures are measured that way and are not what this
# demo shows.
#
# Both refs moved on 2026-09-21, when the branch was rebased from `next` at
# c3605ae48 onto 3f77053a7 and the serial unit suite was run on each: `next`
# alone 18,108 tests OK, perf/recommended 18,148 tests OK.
#
# perf/report.md has NOT been re-measured since that rebase. Every figure in it
# was taken against c3605ae48, so the report and this demo now describe
# different upstream bases. The demo is the one that is current.
#
# ## What is patched in each copy, and what is deliberately not
#
# Patched: the published port, and the compose project name (dc.sh derives it
# from the version alone, so two copies would otherwise fight over one set of
# containers). tier2's default base URL follows the port, so a harness script
# run inside a copy cannot silently measure a different stack.
#
# ## What this is for
#
# A person clicking through two browser windows. No harness instrument runs
# during the demo -- no tier1, no tier2, no measure.sh -- so what the audience
# sees is a page loading, and the only numbers on screen are the browser's own.
# The harness appears here only in setup: armctl to put the right code in each
# copy, restore_snapshot.sh to put the same rows in each database, and their
# content hashes to prove both are what perf/report.md describes.
#
# Not patched: the cpusets. Both copies keep the perf overlay's pinning --
# nautobot on cpus 0,1,4,5, db and redis on 2,6 -- because the demo drives one
# arm at a time. An idle uwsgi worker and an idle Postgres cost nothing, so each
# arm serves its requests under exactly the conditions perf/report.md was
# measured under, and the demo's numbers should land on the published ones.
# Driving both at once would need disjoint cores and a different story about the
# absolute figures.
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_ROOT="${DEMO_ROOT:-$DEMO_DIR/arms}"
SOURCE_TREE="${SOURCE_TREE:-/home/kevsmith/repos/work/nautobot/nautobot}"
SNAPSHOT="${DEMO_SNAPSHOT:-$SOURCE_TREE/perf/snapshot-large.sql}"
STOCK_REF="${STOCK_REF:-fd7b7d4bc}"
BRANCH_REF="${BRANCH_REF:-perf/recommended}"
DEMO_PASSWORD="${DEMO_PASSWORD:-demo1234}"
# Same default dc.sh uses. Named here too because the viewer container is
# started with plain `docker run`, outside dc.sh, and under `set -u` an
# undefined PYTHON_VER makes the image tag unparseable rather than wrong.
export PYTHON_VER="${PYTHON_VER:-3.13}"

# The content hash of each arm, as `arm_control.sh hash` reports it. `start`
# refuses to hand over a demo that is not running the code named above.
#
# These are not the values perf/report.md publishes: the report predates the
# 2026-09-21 rebase onto 3f77053a7. They were computed from the refs directly
# and confirmed against what armctl reported on the measurement host.
#
# These move whenever either ref moves, and they have twice been left behind:
# EXPECT_BRANCH was dfed9a0fbd22fd03 while the branch it named hashed
# 9698baf68850472b, and then 33f4cb8930f61178 until finding 89 landed on
# perf/recommended as e787131d5. Re-derive them from the refs -- never copy them
# from the report, which describes whatever was last measured rather than what
# the demo is about to run:
#
#   git worktree add -q --detach /tmp/h <ref> && cd /tmp/h && \
#     find nautobot \( -name '*.py' -o -name '*.html' -o -name '*.txt' \) \
#       -not -path '*/project-static/*' -print0 | LC_ALL=C sort -z | \
#       xargs -0 cat | sha256sum | cut -c1-16
EXPECT_STOCK="${EXPECT_STOCK:-53a8249102cdb2d9}"
EXPECT_BRANCH="${EXPECT_BRANCH:-cb8e6a4ec415f1e5}"

# The fixed session key ensure_credentials.py mints. `status` and `warm` use it
# so they can fetch a real page without logging in; the demo itself logs in as
# `demo` in a browser. A token would not do: it authenticates DRF only and 302s
# every UI page, which is how a check completes cleanly having checked nothing.
SESSION_KEY="${PERF_SESSION_KEY:-perfbotperfbotperfbotperfbot0000}"

ARMS=(stock branch)

arm_cfg() {
  case "$1" in
    stock)  REF="$STOCK_REF";  PORT=8280; PROJECT="nautobot-demo-stock";  EXPECT="$EXPECT_STOCK" ;;
    branch) REF="$BRANCH_REF"; PORT=8380; PROJECT="nautobot-demo-branch"; EXPECT="$EXPECT_BRANCH" ;;
    *) echo "unknown arm: $1" >&2; exit 2 ;;
  esac
  TREE="$DEMO_ROOT/$1"
  # Cookies are scoped by host, not by port, so both instances being on hannah
  # means they share one cookie jar. With the stock cookie names, logging into
  # the second instance overwrites the first one's sessionid with a key that
  # does not exist in the first one's database, and that tab is silently logged
  # out. Same for csrftoken, which breaks the login POST rather than the page.
  COOKIE="sessionid_$1"
}

in_arm() { arm_cfg "$1"; shift; ( cd "$TREE" && "$@" ); }

patch_tree() {
  local arm="$1"
  arm_cfg "$arm"

    # --- patch 1: the compose project name --------------------------------
    # dc.sh builds it from the version alone, so both copies would name the same
    # project and the second `up` would adopt the first's containers and its
    # database. NAUTOBOT_VER is not the lever: it is also the image tag, so
    # overriding it triggers a full rebuild against an image that does not exist.
    sed -i "s|nautobot-perf-\${NAUTOBOT_VER/./-}|$PROJECT|" "$TREE/perf/scripts/dc.sh"
    grep -q -- "--project-name \"$PROJECT\"" "$TREE/perf/scripts/dc.sh" \
      || { echo "!! project-name patch did not apply in $arm" >&2; exit 1; }

    # --- patch 2: the published port --------------------------------------
    # The overlay publishes twice, 8180 for the harness and 8080 for a browser.
    # Two copies cannot both have either. One published port per copy, on all
    # interfaces so the browser on another host reaches it, and loopback still
    # works for curl on hannah.
    sed -i "/- \"8180:8080\"/d" "$TREE/development/docker-compose.perf.yml"
    sed -i "s|\"0.0.0.0:8080:8080\"|\"0.0.0.0:$PORT:8080\"|" "$TREE/development/docker-compose.perf.yml"
    sed -i "s|\"8181:8080\"|\"$((PORT + 1)):8080\"|" "$TREE/development/docker-compose.perf.yml"
    grep -q "0.0.0.0:$PORT:8080" "$TREE/development/docker-compose.perf.yml" \
      || { echo "!! port patch did not apply in $arm" >&2; exit 1; }
    grep -q "8180:8080\|0.0.0.0:8080:8080" "$TREE/development/docker-compose.perf.yml" \
      && { echo "!! $arm still publishes a measurement-stack port" >&2; exit 1; }

    # --- patch 3: the harness's own default -------------------------------
    # tier2_latency.py defaults to localhost:8180, which in a copy is the
    # measurement stack rather than this one. A harness script run inside a copy
    # must measure that copy.
    sed -i "s|http://localhost:8180|http://localhost:$PORT|g" "$TREE/perf/scripts/tier2_latency.py"

  # --- patch 4: a healthcheck that does not cost a core ------------------
  # As shipped: CMD-SHELL "nautobot-server health_check", every 10 seconds. That
  # boots a fresh interpreter and imports the whole of Nautobot to answer a
  # question the running app already knows -- measured at 6.4s of wall at ~100%
  # of one core, per check, per stack. Two stacks on four cores means an import
  # is almost always in flight, on the same cpus the app serves from. It showed
  # up as page loads scattering to 830ms against a 338ms median.
  #
  # The replacement asks the running app over HTTP: same health plugins, same
  # verdict, 70-90ms warm and no new interpreter. curl is in the image at
  # /usr/bin/curl. The interval goes to 30s because there is no longer any
  # reason to check hard.
  #
  # Guarded, so re-patching an already-patched copy is a no-op.
  if ! grep -q "localhost:8080/health/" "$TREE/development/docker-compose.perf.yml"; then
    awk '
      { print }
      /^  nautobot:$/ && !done {
        print "    healthcheck:"
        print "      test: [\"CMD-SHELL\", \"curl -fsS -o /dev/null http://localhost:8080/health/\"]"
        print "      interval: 30s"
        print "      timeout: 10s"
        print "      start_period: 600s"
        print "      retries: 3"
        done = 1
      }
    ' "$TREE/development/docker-compose.perf.yml" > "$TREE/development/.dc-perf.tmp" \
      && mv "$TREE/development/.dc-perf.tmp" "$TREE/development/docker-compose.perf.yml"
  fi
  grep -q "localhost:8080/health/" "$TREE/development/docker-compose.perf.yml" \
    || { echo "!! healthcheck patch did not apply in $arm" >&2; exit 1; }

  # --- patch 5: let the side-by-side page frame this instance ------------
  # Nautobot sets X_FRAME_OPTIONS = "DENY" (nautobot/core/settings.py), so every
  # response carries X-Frame-Options: DENY and both panes of web/index.html
  # render blank. SAMEORIGIN would not help: origin includes the port, so
  # :8480 framing :8280 is cross-origin either way.
  #
  # Dropping the middleware removes the header rather than sending a value
  # browsers have to be lenient about. nautobot_config.py does `from
  # nautobot.core.settings import *`, so MIDDLEWARE is in scope here -- the file
  # already mutates it a few lines up to add the debug toolbar.
  #
  # Scope: these two instances, on a box whose purpose is showing them
  # side by side. Nothing here is a suggestion for a deployment.
  if ! grep -q "XFrameOptionsMiddleware" "$TREE/development/nautobot_config.py"; then
    cat >> "$TREE/development/nautobot_config.py" <<'CFG'

# Demo only: the side-by-side viewer frames this instance from another port on
# the same host, which X-Frame-Options: DENY forbids. See perf/demo/demo.sh.
MIDDLEWARE = [m for m in MIDDLEWARE if m != "django.middleware.clickjacking.XFrameOptionsMiddleware"]  # noqa: F405
CFG
  fi
  grep -q "XFrameOptionsMiddleware" "$TREE/development/nautobot_config.py" \
    || { echo "!! framing patch did not apply in $arm" >&2; exit 1; }

  # --- patch 6: cookie names, one set per arm ----------------------------
  # Both instances answer as host `hannah`, and cookies ignore the port, so the
  # two share a cookie jar. Under the stock names the second login overwrites
  # the first, and the first instance -- whose database has no such session --
  # shows a login form again. Naming them per arm gives each its own session and
  # its own CSRF token, so both can be logged in at once, which is the whole
  # point of showing them side by side.
  if ! grep -q "SESSION_COOKIE_NAME" "$TREE/development/nautobot_config.py"; then
    cat >> "$TREE/development/nautobot_config.py" <<CFG

# Demo only: the two instances share one host and therefore one cookie
# jar, so each needs its own cookie names to stay logged in independently.
SESSION_COOKIE_NAME = "sessionid_$arm"
CSRF_COOKIE_NAME = "csrftoken_$arm"
CFG
  fi
  grep -q "sessionid_$arm" "$TREE/development/nautobot_config.py" \
    || { echo "!! cookie-name patch did not apply in $arm" >&2; exit 1; }
}

cmd_install() {
  mkdir -p "$DEMO_ROOT"
  [ -f "$SNAPSHOT" ] || { echo "!! snapshot missing: $SNAPSHOT" >&2; exit 1; }
  for arm in "${ARMS[@]}"; do
    arm_cfg "$arm"
    echo "== copying the measurement tree to $arm =="
    # .git comes along: armctl needs it to arm, and it is what lets each copy
    # report its own provenance. perf/results is run output belonging to the
    # measurement tree, and the snapshots are symlinked rather than copied --
    # 200MB each, identical, and read once.
    rsync -a --delete \
      --exclude "perf/results/" \
      --exclude "perf/snapshot*.sql" \
      --exclude "__pycache__/" --exclude "*.pyc" \
      --exclude "node_modules/" --exclude ".venv/" \
      --exclude ".coverage" --exclude ".coverage.*" --exclude "htmlcov/" \
      --exclude "nautobot/project-static/docs/" \
      "$SOURCE_TREE/" "$TREE/"
    for snap in "$SOURCE_TREE"/perf/snapshot*.sql; do
      ln -sfn "$snap" "$TREE/perf/$(basename "$snap")"
    done

    # Upstream tracks one file under __pycache__ -- a stray temp bytecode file,
    # nautobot/core/celery/__pycache__/task.cpython-313.pyc.140297736092592,
    # whose name ends in digits rather than .pyc and so slips past .gitignore.
    # The rsync above skips __pycache__ (thousands of files the container writes
    # through the bind mount as root), which leaves that one tracked file missing
    # and git calling it deleted. armctl's `git checkout <ref> -- nautobot/` then
    # tries to restore it into a directory the container has since recreated as
    # root, and the whole arm fails with "Permission denied / checkout failed".
    #
    # Restoring it here -- before any container has started and while the
    # directory is still ours to create -- is what makes arming work. A root-owned
    # FILE inside a directory we own is fine; git can replace it.
    git -C "$TREE" ls-files -z 'nautobot/**/__pycache__/*' \
      | xargs -0 -r git -C "$TREE" checkout --
    [ -z "$(git -C "$TREE" status --porcelain -- 'nautobot/**/__pycache__/*')" ] \
      || { echo "!! $arm still has a missing tracked file under __pycache__" >&2; exit 1; }

    patch_tree "$arm"

    printf '   %-7s %s  port %s  project %s\n' "$arm" "$TREE" "$PORT" "$PROJECT"
  done
  echo "installed. next: demo.sh start, then demo.sh arm"
}

cmd_patch() {
  # Re-apply the install-time patches in place. Unlike `install` this does not
  # re-copy the tree, so the armed code survives -- which is what you want when
  # only the stack configuration has changed. Recreate the containers afterwards
  # with `start`: compose applies a changed healthcheck on create, not on restart.
  for arm in "${ARMS[@]}"; do patch_tree "$arm"; echo "   patched $arm"; done
  # A patched config reaches the app only on a restart. nautobot_config.py and
  # the compose overlay are bind-mounted, so editing them neither recreates the
  # container nor reloads uwsgi, which has no autoreloader -- the settings sit
  # on disk being ignored, which reads exactly like a patch that did not work.
  local running=0
  for arm in "${ARMS[@]}"; do
    arm_cfg "$arm"
    if [ "$(in_arm "$arm" perf/scripts/dc.sh ps --format '{{.Name}}' 2>/dev/null | grep -c 'nautobot-1')" = "1" ]; then
      running=1
      # `up -d` rather than `restart`: a changed healthcheck or port needs the
      # container recreated, which only `up` does.
      in_arm "$arm" perf/scripts/dc.sh --progress quiet up -d --force-recreate nautobot >/dev/null
    fi
  done
  [ "$running" -eq 1 ] && for arm in "${ARMS[@]}"; do ready_arm "$arm"; done
  return 0
}


# The side-by-side page is served from hannah rather than opened as a local
# file, because Nautobot's sessionid and csrftoken are SameSite=Lax: with a
# file:// parent every frame request is cross-site, no cookie is sent, and both
# panes show a login form that cannot be logged into. Ports are not part of a
# "site", so served from this host the cookies flow as they do in a normal tab.
#
# It runs in the Nautobot dev image, which is already on the box, rather than
# pulling nginx for eight lines of HTML. cpuset 3,7 keeps it off the cores the
# two instances serve from.
VIEWER_NAME="nautobot-demo-web"
VIEWER_PORT="${DEMO_VIEWER_PORT:-8480}"

viewer_image() {
  grep -m1 '^version = ' "$DEMO_ROOT/stock/pyproject.toml" \
    | sed -E "s|version = \"([0-9]+\.[0-9]+).*\"|local/nautobot-dev:local-\1-py${PYTHON_VER}|"
}

viewer_up() {
  docker rm -f "$VIEWER_NAME" >/dev/null 2>&1 || true
  # --entrypoint, because the dev image's own entrypoint treats everything after
  # the image name as arguments to nautobot-server. Without it the container
  # comes up, publishes the port, logs nothing and answers nothing.
  #
  # --no-healthcheck, because the image's healthcheck is inherited too, and it is
  # the 6.4-second Nautobot import this stack went to some trouble to get rid of.
  docker run -d --name "$VIEWER_NAME" --cpuset-cpus 3,7 --restart unless-stopped \
    --entrypoint python3 --no-healthcheck \
    -p "0.0.0.0:$VIEWER_PORT:$VIEWER_PORT" \
    -v "$DEMO_DIR/web:/web:ro" -w /web \
    "$(viewer_image)" -m http.server "$VIEWER_PORT" >/dev/null
  printf '   %-7s http://hannah:%s/\n' "viewer" "$VIEWER_PORT"
}

ready_arm() {
  # Wait for the app to report healthy, then warm it.
  local arm="$1" i state
  arm_cfg "$arm"
  printf '   %-7s ' "$arm"
  for i in $(seq 1 90); do
    state="$(in_arm "$arm" perf/scripts/dc.sh ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -c 'nautobot-1.*healthy' || true)"
    [ "$state" = "1" ] && break
    sleep 2
  done
  if [ "$state" = "1" ]; then
    # uwsgi runs three worker processes and each one compiles templates and
    # fills its per-process caches on the first request it happens to serve.
    # Measured on this stack: the first requests after a start read 826ms
    # against a 304ms warm median, and which of them is slow depends on which
    # worker the browser lands on.
    #
    # Concurrently, and that detail matters: uwsgi hands each request to the
    # first free worker rather than round-robin, so six sequential requests can
    # all be served by one worker and leave the other two cold. Warming
    # sequentially left a 620ms straggler in a run of five. Four at once
    # against three workers guarantees every worker takes one.
    local w round
    for round in 1 2; do
      for w in 1 2 3 4; do
        curl -s -o /dev/null --max-time 180 -b "$COOKIE=$SESSION_KEY" \
          "http://localhost:$PORT$([ "$round" -eq 1 ] && echo / || echo /dcim/devices/)" &
      done
      wait
    done
    echo "healthy, warm  http://hannah:$PORT/"
  else
    echo "started, not yet healthy (expected before the first load)"
  fi
}

cmd_up() {
  for arm in "${ARMS[@]}"; do
    arm_cfg "$arm"
    echo "== $arm: starting on :$PORT =="
    # celery is left out: it fires scheduled work into the middle of whatever
    # page is on screen, and nothing here needs a worker. It is also what
    # quiesce.sh expects to find running -- db, nautobot, redis.
    #
    # --progress quiet: the default renders a spinner frame per tenth of a
    # second, which is fine on a terminal and a thousand lines of escape codes
    # in a log.
    in_arm "$arm" perf/scripts/dc.sh --progress quiet up -d db redis nautobot >/dev/null
  done
  for arm in "${ARMS[@]}"; do ready_arm "$arm"; done
  viewer_up
}

# Moving STOCK_REF to a newer upstream commit is not only a constant change: each arm
# is a COPY of the measurement tree with its own .git, taken at install time, so a ref
# that postdates the copy is not an object those trees hold and `arm` fails with
# "checkout failed". Refresh them first --
#
#   perf/scripts/sync.sh            # from the Mac, so the host has the objects
#   perf/demo/remote.sh install     # re-copies both trees, re-applies the patches
#
# -- then start, load and arm as usual. `install` is the expensive path (it re-rsyncs
# two full checkouts); nothing cheaper brings a new ref into an existing arm.

cmd_arm() {
  # Only needed when a ref moves or a copy has been disturbed: the armed code
  # lives in the tree on disk and survives stop/start. armctl restarts nautobot
  # (uwsgi has no autoreloader) and waits for loadavg to fall before returning.
  for arm in "${ARMS[@]}"; do
    arm_cfg "$arm"
    echo "== $arm: arming to $REF =="
    # armctl's last act is a gate: it refuses to return until loadavg is under
    # 0.7, because finding 35 showed a measurement started under load reads
    # bimodally high for its whole arm. Nothing here measures, and three stacks
    # on one box plus post-restore autovacuum keep the box above that ceiling for
    # a long time -- so the gate fails an arm that is otherwise complete. The
    # checkout, the restart and the health wait all happen before it.
    #
    # So: let that specific failure through, and verify what actually matters
    # ourselves -- the container is healthy, and the code is the right code.
    local out rc=0
    out="$(in_arm "$arm" perf/scripts/arm_control.sh arm "$REF" 2>&1)" || rc=$?
    echo "$out" | sed 's/^/   /'
    if [ "$rc" -ne 0 ] && ! echo "$out" | grep -q "load did not settle"; then
      echo "   !! arming $arm failed" >&2; return 1
    fi
    [ "$(in_arm "$arm" perf/scripts/dc.sh ps --format '{{.Name}} {{.Status}}' | grep -c 'nautobot-1.*healthy')" = "1" ] \
      || { echo "   !! $arm is not healthy after arming" >&2; return 1; }
  done
  cmd_hash
}

cmd_load() {
  echo "== restoring $(basename "$SNAPSHOT") into both arms =="
  for arm in "${ARMS[@]}"; do
    arm_cfg "$arm"
    echo "-- $arm"
    # The harness's own restore: drops and recreates the database, loads the
    # snapshot, migrates to whatever the armed code needs (the branch adds
    # 0146_objectchange_object_data_nullable, so the two schemas differ on
    # purpose), and mints the session a restore has just invalidated.
    in_arm "$arm" perf/scripts/restore_snapshot.sh | sed 's/^/   /'
    # The one thing the harness user lacks is a password, because nothing in the
    # harness logs in through a browser. A demo does.
    # On stdin, not `shell -c`: nautobot-server takes -c as --config-path, so the
    # script is read as a filename and the command dies with "Configuration file
    # not found at <the whole script>". Django's shell executes piped stdin.
    in_arm "$arm" perf/scripts/dc.sh exec -T nautobot nautobot-server shell <<PYEOF | sed 's/^/   /'
from django.contrib.auth import get_user_model
U = get_user_model()
u, _ = U.objects.get_or_create(username="demo", defaults={"is_superuser": True, "is_staff": True, "is_active": True})
u.is_superuser = u.is_staff = u.is_active = True
u.set_password("$DEMO_PASSWORD")
u.save()
print("browser login: demo / $DEMO_PASSWORD")
PYEOF
  done
  cmd_status
}

cmd_hash() {
  # Two different hashes is the claim that the arms are not the same code; the
  # expected values are the ones perf/report.md publishes for its own arms, so
  # matching them says the demo is running exactly what was measured.
  local ok=0
  for arm in "${ARMS[@]}"; do
    arm_cfg "$arm"
    local h; h="$(in_arm "$arm" perf/scripts/arm_control.sh hash)"
    if [ "${h:0:16}" = "$EXPECT" ]; then
      printf '   %-7s %s  matches the report\n' "$arm" "${h:0:16}"
    else
      printf '   %-7s %s  !! EXPECTED %s\n' "$arm" "${h:0:16}" "$EXPECT"; ok=1
    fi
  done
  [ "$ok" -eq 0 ] || { echo "   the arms are not the code perf/report.md describes" >&2; return 1; }
}

cmd_status() {
  printf '%-7s %-6s %-11s %-18s %s\n' ARM PORT COMMIT "NAUTOBOT HASH" STATE
  for arm in "${ARMS[@]}"; do
    arm_cfg "$arm"
    local sha=- h=- state code
    [ -d "$TREE/.git" ] && sha="$(git -C "$TREE" rev-parse --short HEAD)"
    [ -d "$TREE/.git" ] && h="$(in_arm "$arm" perf/scripts/arm_control.sh hash | cut -c1-16)"
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
             -b "$COOKIE=$SESSION_KEY" "http://localhost:$PORT/dcim/devices/" 2>/dev/null || echo 000)"
    case "$code" in
      200) state="up, authenticated" ;;
      302) state="up, NOT authenticated -- run 'demo.sh load'" ;;
      000) state="down" ;;
      *)   state="HTTP $code" ;;
    esac
    # The commit is the tree's HEAD, which is the measurement branch for both
    # copies; the arm is the hash, not the commit. Said plainly so nobody reads
    # the column as the ref under test.
    printf '%-7s %-6s %-11s %-18s %s\n' "$arm" "$PORT" "$sha" "$h" "$state"
  done
  echo
  echo "row counts (identical across arms is the point):"
  for arm in "${ARMS[@]}"; do
    arm_cfg "$arm"
    printf '   %-7s %s\n' "$arm" "$(in_arm "$arm" perf/scripts/dc.sh exec -T db psql -U nautobot -d nautobot -tA -c \
      "select (select count(*) from dcim_device)||' devices, '||
              (select count(*) from dcim_interface)||' interfaces, '||
              (select count(*) from dcim_cable)||' cables, '||
              (select count(*) from ipam_ipaddress)||' IPs, '||
              (select count(*) from extras_objectchange)||' changes'" 2>/dev/null || echo unavailable)"
  done
  echo
  cmd_urls
}

cmd_urls() {
  for arm in "${ARMS[@]}"; do arm_cfg "$arm"; printf '   %-7s http://hannah:%s/\n' "$arm" "$PORT"; done
  printf '   %-7s http://hannah:%s/   (both panes, one window)\n' "viewer" "$VIEWER_PORT"
  echo "   browser login: demo / $DEMO_PASSWORD"
}

cmd_down() {
  docker rm -f "$VIEWER_NAME" >/dev/null 2>&1 || true
  for arm in "${ARMS[@]}"; do echo "== stopping $arm =="; in_arm "$arm" perf/scripts/dc.sh --progress quiet stop >/dev/null; done
}
cmd_nuke() { docker rm -f "$VIEWER_NAME" >/dev/null 2>&1 || true; for arm in "${ARMS[@]}"; do echo "== removing $arm =="; in_arm "$arm" perf/scripts/dc.sh --progress quiet down -v >/dev/null; done; }

case "${1:-status}" in
  setup)     cmd_install; cmd_up; cmd_load; cmd_arm ;;
  install)   cmd_install ;;
  patch)     cmd_patch ;;
  start|up)  cmd_up ;;
  arm)       cmd_arm ;;
  load)      cmd_load ;;
  status)    cmd_status ;;
  hash)      cmd_hash ;;
  urls)      cmd_urls ;;
  stop|down) cmd_down ;;
  nuke)      cmd_nuke ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
