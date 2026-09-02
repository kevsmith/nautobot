#!/usr/bin/env bash
# Record the loaded dataset's object counts so run_experiment.sh can detect drift.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
cat <<'SNIP' | perf/dc.sh exec -T nautobot nautobot-server shell 2>/dev/null | tail -1 > perf/baselines/expected-counts.txt
from nautobot.dcim.models import Device, Interface, Cable
from nautobot.ipam.models import IPAddress
print(Device.objects.count(), Interface.objects.count(), Cable.objects.count(), IPAddress.objects.count())
SNIP
echo "recorded: $(cat perf/baselines/expected-counts.txt)"
