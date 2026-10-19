"""Per endpoint and depth: rows, queries, a canonical-JSON content hash, and median wall clock.

perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_depth.py --reps 5 /api/dcim/interfaces/ ...
"""

import argparse
import json
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, "/source/perf/scripts")
from tier1_queries import fingerprint, get_perf_client  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--reps", type=int, default=5)
ap.add_argument("--depths", default="0,1,2,3")
ap.add_argument("--limit", type=int, default=100)
ap.add_argument("urls", nargs="+")
args = ap.parse_args()

client = get_perf_client()
out = []
for base in args.urls:
    for depth in [int(d) for d in args.depths.split(",")]:
        url = f"{base}?limit={args.limit}&depth={depth}"
        client.get(url)  # warm-up, discarded
        connection.queries_log.clear()
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url)
        # Read now: `captured_queries` slices the live log, which the timing reps below keep appending to.
        query_count = len(ctx.captured_queries)
        body = json.loads(resp.content)
        content_hash, _ = fingerprint(resp.content, resp["Content-Type"])
        walls = []
        for _ in range(args.reps):
            t = time.perf_counter()
            client.get(url)
            walls.append((time.perf_counter() - t) * 1000.0)
        rec = {
            "url": url,
            "status": resp.status_code,
            "rows": len(body.get("results", [])),
            "queries": query_count,
            "content_hash": content_hash,
            "bytes": len(resp.content),
            "wall_ms": round(statistics.median(walls), 1),
        }
        out.append(rec)
        print(json.dumps(rec), file=sys.stderr, flush=True)
print(json.dumps(out))
