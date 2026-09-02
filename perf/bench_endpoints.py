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
from tier1w_writes import ConfigCallCounter  # noqa: E402
from django.core.cache import caches as _django_caches  # noqa: E402


class RedisReadCounter:
    """Count reads that reach the cache backend.

    ConfigCallCounter counts calls to get_settings_or_config(); once that function
    memoizes internally, its call count stops tracking actual Redis traffic. This
    counts one layer lower, at the cache backend, which is what actually costs a
    network round trip.
    """

    def __init__(self):
        self.count = 0
        self._orig = None
        self._backend_cls = None

    def __enter__(self):
        # django.core.cache.cache is a ConnectionProxy; patch the real backend class.
        self._backend_cls = type(_django_caches["default"])
        self._orig = self._backend_cls.get

        def counting(cache_self, *a, **kw):
            self.count += 1
            return self._orig(cache_self, *a, **kw)

        self._backend_cls.get = counting
        return self

    def __exit__(self, *exc):
        self._backend_cls.get = self._orig

# The endpoints where serialization dominates; no point timing cheap ones.
TARGETS = [
    "api.interface.list", "api.interface.depth1", "api.device.list",
    "api.device.list.depth1", "api.prefix.list", "api.ipaddress.list",
    "api.location.list", "api.cable.list", "ui.device.list", "ui.interface.list",
    # Table-rendering heavy pages -- these are where per-cell template work shows up.
    "ui.device.interfaces", "ui.device.detail", "ui.rack.detail", "ui.cable.list",
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
        # Redis/Constance reads are deterministic and load-independent, unlike wall
        # clock. For fixes that remove config lookups rather than SQL, this is the
        # signal that survives a busy machine.
        counter = ConfigCallCounter()
        redis_counter = RedisReadCounter()
        with counter, redis_counter:
            client.get(url)
        config_reads = counter.count
        redis_reads = redis_counter.count

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
            "config_reads": config_reads,
            "redis_reads": redis_reads,
            "reps": len(samples),
        }
        print(f"{tid:28s} cfg={out[tid]['config_reads']:<5} redis={out[tid]['redis_reads']:<5} "
              f"median={out[tid]['median_ms']:>9.2f}ms  "
              f"(p10={out[tid]['p10_ms']:.0f} p90={out[tid]['p90_ms']:.0f})", file=sys.stderr)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
