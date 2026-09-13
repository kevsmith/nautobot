#!/usr/bin/env python
"""Characterize the intermittent pause that biased finding 31's create.device reading.

tier1w_writes.py reports the LAST of its reps. If a pause recurs on a fixed period
in call count, whichever rep it lands on is reported, and it lands consistently
across independent runs of the same tree -- which is how create.device read 89ms on
one arm and 248ms on the other for three rounds running, while both arms actually
cost the same.

    perf/dc.sh exec -T nautobot python /source/perf/probe_f31_device.py [--gc-off]
"""

import argparse
import gc
import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier1w_writes as t1w  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--gc-off", action="store_true", help="disable the cyclic collector")
ap.add_argument("--n", type=int, default=24)
args = ap.parse_args()

if args.gc_off:
    gc.disable()

user_model = get_user_model()
user = user_model.objects.filter(username=t1w.PERF_USER).first()
fixtures = t1w.fixtures()
op = dict(t1w.OPERATIONS)["create.device"]


def once():
    try:
        with transaction.atomic():
            with t1w.web_request_context(user, context_detail="perf-probe"):
                op(fixtures)
            raise t1w.Rollback
    except t1w.Rollback:
        pass


gen2_before = gc.get_stats()[2]["collections"]
timings = []
gen2 = []
for _ in range(args.n):
    start = time.perf_counter()
    once()
    timings.append((time.perf_counter() - start) * 1000.0)
    gen2.append(gc.get_stats()[2]["collections"])

print(f"gc {'disabled' if args.gc_off else 'enabled'}, {args.n} calls")
for i, (t, g) in enumerate(zip(timings, gen2), start=1):
    mark = "  <-- gen2 collection" if g > (gen2[i - 2] if i > 1 else gen2_before) else ""
    print(f"  call {i:2d} {t:7.1f}ms{mark}")
ordered = sorted(timings)
print(f"median {ordered[len(ordered) // 2]:.1f}ms  min {ordered[0]:.1f}ms  max {ordered[-1]:.1f}ms")
