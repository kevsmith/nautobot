#!/usr/bin/env python
"""Measure what writing ObjectChange.object_data costs on the current tree.

Finding 16 measured the v1 serializer at 0.73ms and 2 queries per record, worth
~14% of bulk.create.loop -- but that was before several accepted changes, and
finding 27 established that a stated prize can go stale. Re-measure before
designing anything.

    perf/dc.sh exec -T nautobot python /source/perf/probe_v1_cost.py
"""

import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier1w_writes as t1w  # noqa: E402

from nautobot.extras.models import change_logging  # noqa: E402

stats = {"v1_calls": 0, "v1_s": 0.0, "v2_calls": 0, "v2_s": 0.0}


def wrap(module, name, key):
    original = getattr(module, name)

    def counting(*args, **kwargs):
        start = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            stats[f"{key}_calls"] += 1
            stats[f"{key}_s"] += time.perf_counter() - start

    setattr(module, name, counting)


# change_logging imports both names directly, so patch them where they are used.
wrap(change_logging, "serialize_object", "v1")
wrap(change_logging, "serialize_object_v2", "v2")

user_model = get_user_model()
user = user_model.objects.filter(username=t1w.PERF_USER).first()
if user is None:
    user = user_model.objects.create(username=t1w.PERF_USER, is_superuser=True, is_staff=True, is_active=True)
fixtures = t1w.fixtures()

TARGETS = {
    f"bulk.create.interfaces.x{t1w.BULK_N}.loop",
    f"bulk.create.interfaces.x{t1w.BULK_N}.deferred",
    f"bulk.update.interfaces.x{t1w.BULK_N}.deferred",
    f"bulk.delete.interfaces.x{t1w.BULK_N}",
}

print(f"{'operation':40s} {'wall':>9} {'v1 calls':>9} {'v1 ms':>8} {'share':>7} {'v2 ms':>8}")
for name, fn in t1w.OPERATIONS:
    if name not in TARGETS:
        continue
    for key in stats:
        stats[key] = 0 if key.endswith("calls") else 0.0
    rec = t1w.measure(user, name, fn, fixtures)
    wall = rec["wall_ms"]
    v1_ms = stats["v1_s"] * 1000.0
    v2_ms = stats["v2_s"] * 1000.0
    print(
        f"{name:40s} {wall:8.1f}ms {stats['v1_calls']:9d} {v1_ms:7.1f}ms "
        f"{(v1_ms / wall * 100.0) if wall else 0:6.1f}% {v2_ms:7.1f}ms"
    )

print("\nv1 is the half this experiment would remove; v2 is the half that stays.")
