#!/usr/bin/env bash
# Thin wrapper around this stack's docker compose invocation, so the perf
# scripts and ad-hoc commands can't drift onto the wrong project or ports.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHON_VER="${PYTHON_VER:-3.13}"
# Derive the version from pyproject.toml exactly as tasks.py does. Hardcoding
# it silently points every command at the wrong compose project after a
# branch switch.
export NAUTOBOT_VER="${NAUTOBOT_VER:-$(grep -m1 '^version = ' "$ROOT/pyproject.toml" | sed -E 's/version = "([0-9]+\.[0-9]+).*"/\1/')}"
exec docker compose \
  --project-name "nautobot-perf-${NAUTOBOT_VER/./-}" \
  --project-directory "$ROOT/development/" \
  -f "$ROOT/development/docker-compose.yml" \
  -f "$ROOT/development/docker-compose.postgres.yml" \
  -f "$ROOT/development/docker-compose.dev.yml" \
  -f "$ROOT/development/docker-compose.perf.yml" \
  "$@"
