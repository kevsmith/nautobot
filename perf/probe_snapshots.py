#!/usr/bin/env python
"""Measure what batching get_prev_change would be worth, before building it.

Finding 27 proposes batching the per-ObjectChange get_prev_change() query in the
deferred change-logging flush. The implementation is careful -- it has to handle
an object changed twice in one request, and the clean batch form is
PostgreSQL-only -- so the prize is worth measuring before the effort is spent.

Wraps get_prev_change to count calls and accumulate time, then runs the same
write operations Tier 1W runs, so the figures sit next to numbers already on
record.

    perf/dc.sh exec -T nautobot python /source/perf/probe_snapshots.py
"""

import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier1w_writes as t1w  # noqa: E402

from nautobot.extras.models.change_logging import ObjectChange  # noqa: E402

TARGETS = {
    f"bulk.update.interfaces.x{t1w.BULK_N}.deferred",
    f"bulk.delete.interfaces.x{t1w.BULK_N}",
    f"bulk.create.interfaces.x{t1w.BULK_N}.deferred",
    "delete.device",
}

stats = {"calls": 0, "seconds": 0.0}
_original = ObjectChange.get_prev_change


def counting(self, *args, **kwargs):
    start = time.perf_counter()
    try:
        return _original(self, *args, **kwargs)
    finally:
        stats["calls"] += 1
        stats["seconds"] += time.perf_counter() - start


ObjectChange.get_prev_change = counting

from django.contrib.auth import get_user_model  # noqa: E402

User = get_user_model()
user = User.objects.filter(username=t1w.PERF_USER).first()
if user is None:
    user = User.objects.create(username=t1w.PERF_USER, is_superuser=True, is_staff=True, is_active=True)
fx = t1w.fixtures()
print(f"{'operation':44s} {'wall':>9} {'calls':>7} {'in gpc':>9} {'share':>7}")
for name, fn in t1w.OPERATIONS:
    if name not in TARGETS:
        continue
    stats["calls"], stats["seconds"] = 0, 0.0
    rec = t1w.measure(user, name, fn, fx)
    wall = rec["wall_ms"]
    gpc = stats["seconds"] * 1000.0
    share = (gpc / wall * 100.0) if wall else 0.0
    print(f"{name:44s} {wall:8.1f}ms {stats['calls']:7d} {gpc:8.1f}ms {share:6.1f}%")
