#!/usr/bin/env python
"""Focused wall-clock A/B for endpoints whose cost is CPU, not queries.

Tier 1 gates on query count, which is blind to a pure serialization fix; Tier 2
goes over HTTP at millisecond resolution with network noise in the way. This
measures in-process, many reps, and reports the median -- the right instrument
for a change that removes Python work without removing a single query.

    python /source/perf/bench_endpoints.py --reps 15 --out /source/perf/results/bench-<name>.json
"""

import argparse
import json
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402
import workload as workload_mod  # noqa: E402

# The endpoints where serialization dominates; no point timing cheap ones.
TARGETS = [
    "api.interface.list", "api.interface.depth1", "api.device.list",
    "api.device.list.depth1", "api.prefix.list", "api.ipaddress.list",
    "api.location.list", "api.cable.list", "ui.device.list", "ui.interface.list",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=15)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    resolved, _ = workload_mod.resolve(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "workload.yml"))
    by_id = {r["id"]: r for r in resolved}
    client = get_perf_client()

    out = {}
    for tid in TARGETS:
        sc = by_id.get(tid)
        if not sc:
            continue
        url = sc["url"]
        for _ in range(3):  # warm caches before timing
            client.get(url)
        samples = []
        for _ in range(args.reps):
            start = time.perf_counter()
            resp = client.get(url)
            samples.append((time.perf_counter() - start) * 1000.0)
        samples.sort()
        out[tid] = {
            "status": resp.status_code,
            "median_ms": round(statistics.median(samples), 2),
            "p10_ms": round(samples[max(0, len(samples) // 10)], 2),
            "p90_ms": round(samples[min(len(samples) - 1, 9 * len(samples) // 10)], 2),
            "reps": len(samples),
        }
        print(f"{tid:28s} median={out[tid]['median_ms']:>9.2f}ms  "
              f"(p10={out[tid]['p10_ms']:.0f} p90={out[tid]['p90_ms']:.0f})", file=sys.stderr)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
