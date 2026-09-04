#!/usr/bin/env python
"""Wall clock for the Tier 1W write operations, as a median over enough reps to survive a GC pause.

`tier1w_writes.py` reports the LAST of its reps, and a gen-2 cyclic collection costs ~200ms and
recurs on a fixed period in allocation count. That period is deterministic for a given tree, so
the pause lands on the reported rep for one arm and not the other, and it does so in every
independent run -- three alternating rounds cannot cancel a bias that is not random. It read
create.device at 89ms on one arm and 248ms on the other while both actually cost ~120ms.

Query counts are unaffected: the collector does not issue SQL. So tier1w stays the deterministic
counter and this is the ranking instrument.

    perf/dc.sh exec -T nautobot python /source/perf/probe_f31_wall.py --reps 9
"""

import argparse
import json
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier1w_writes as t1w  # noqa: E402

OPS = [
    "bulk.create.interfaces.x100.loop",
    "bulk.create.interfaces.x100.deferred",
    "bulk.create.interfaces.x100.bulk_create",
    "bulk.update.interfaces.x100.deferred",
    "bulk.delete.interfaces.x100",
    "create.interfaces.x50",
    "create.device",
    "create.interface",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=9)
    ap.add_argument("--out")
    args = ap.parse_args()

    user_model = get_user_model()
    user = user_model.objects.filter(username=t1w.PERF_USER).first()
    if user is None:
        user = user_model.objects.create(username=t1w.PERF_USER, is_superuser=True, is_staff=True, is_active=True)
    fixtures = t1w.fixtures()
    by_name = dict(t1w.OPERATIONS)

    records = []
    print(f"{'operation':40s} {'median':>9} {'min':>9} {'max':>9} {'spread':>7}")
    for name in OPS:
        fn = by_name[name]

        def once():
            try:
                with transaction.atomic():
                    with t1w.web_request_context(user, context_detail="perf-f31-wall"):
                        fn(fixtures)
                    raise t1w.Rollback
            except t1w.Rollback:
                pass

        once()  # warmup
        timings = []
        for _ in range(args.reps):
            start = time.perf_counter()
            once()
            timings.append((time.perf_counter() - start) * 1000.0)
        median = statistics.median(timings)
        rec = {
            "operation": name,
            "median_ms": round(median, 2),
            "min_ms": round(min(timings), 2),
            "max_ms": round(max(timings), 2),
            "timings_ms": [round(t, 2) for t in timings],
        }
        records.append(rec)
        print(
            f"{name:40s} {median:8.1f}ms {min(timings):8.1f}ms {max(timings):8.1f}ms "
            f"{(max(timings) - min(timings)) / median * 100:6.1f}%"
        )

    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump({"schema": 1, "reps": args.reps, "operations": records}, fh, indent=2, sort_keys=True)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
