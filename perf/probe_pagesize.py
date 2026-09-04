#!/usr/bin/env python
"""Page-size sensitivity for one endpoint, and whether `depth` does anything.

A query count that does not move with `?depth` has two very different
explanations: the cost is genuinely depth-independent, or the endpoint ignores
depth entirely. And a count that does not move with page size is fixed overhead
rather than per-row work. Both are cheap to establish and neither should be
guessed at.

    perf/dc.sh exec -T nautobot python /source/perf/probe_pagesize.py <view-name>
"""

import hashlib
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.urls import reverse  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client, measure  # noqa: E402

view = sys.argv[1] if len(sys.argv) > 1 else "dcim-api:interfaceconnections-list"
base = reverse(view)
client = get_perf_client()

print(f"{view}\n")
print(f"{'limit':>6} {'depth':>6} {'objects':>8} {'queries':>8} {'q/obj':>7} {'dup':>6} {'ms':>9} {'bytes':>10}")
prev = None
for limit in (1, 5, 10, 25, 50):
    for depth in (0, 1):
        url = f"{base}?limit={limit}" + (f"&depth={depth}" if depth else "")
        client.get(url)  # warm
        rec = measure(client, url)
        body = client.get(url)
        n = len((body.json() or {}).get("results", [])) if rec["status"] == 200 else 0
        digest = hashlib.sha256(body.content).hexdigest()[:12]
        print(
            f"{limit:6d} {depth:6d} {n:8d} {rec['query_count']:8d} "
            f"{rec['query_count'] / n if n else 0:7.2f} {rec['duplicate_queries']:6d} "
            f"{rec['wall_ms']:9.1f} {rec['response_bytes']:10d}  {digest}"
        )

print("\nIf the depth=0 and depth=1 digests match at a given limit, the endpoint")
print("ignores depth and 'identical query counts' says nothing about cost class.")
