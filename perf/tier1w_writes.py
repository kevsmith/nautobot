#!/usr/bin/env python
"""Tier 1W: deterministic write-path query profiling.

Tier 1 only exercises GET. Change logging, signals and validation cost live on
the write path -- ObjectChange, for instance, serializes every changed object
twice -- and none of that is visible to a read-only workload.

Each operation runs inside ``web_request_context`` (so change logging and
webhook processing behave as they do for a real write) and inside a transaction
that is **rolled back afterwards**, so every operation starts from identical
state and the run is repeatable. Nautobot creates ObjectChange records from a
synchronous ``post_save``/``m2m_changed`` receiver, so rollback does not hide
change-logging cost.

    nautobot-server shell < /dev/null   # (not needed; this bootstraps itself)
    python /source/perf/tier1w_writes.py --out /source/perf/results/tier1w.json
"""

import argparse
from collections import Counter
import json
import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.db.models import Count  # noqa: E402

from nautobot.dcim.models import Device, DeviceType, Interface, Location  # noqa: E402
from nautobot.extras.context_managers import (  # noqa: E402
    deferred_change_logging_for_bulk_operation,
    web_request_context,
)
from nautobot.extras.models import ObjectChange, Role, Status, Tag  # noqa: E402
import nautobot.core.utils.config as _config_mod  # noqa: E402
from nautobot.ipam.models import IPAddress, Namespace, Prefix  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import normalize_sql  # noqa: E402

PERF_USER = "perfbot"


class Rollback(Exception):
    """Raised to unwind the measurement transaction."""


class ConfigCallCounter:
    """Count get_settings_or_config() calls, each of which reads Constance from Redis.

    Some costs remove no SQL at all -- caching a classproperty that reads config, for instance.
    Wall clock varies ~20% run to run on this box, which is the same order as the effect being
    measured, so this counter provides the deterministic signal that query count provides for SQL.
    """

    def __init__(self):
        self.count = 0
        self._original = None

    def __enter__(self):
        self._original = _config_mod.get_settings_or_config

        def counting(*args, **kwargs):
            self.count += 1
            return self._original(*args, **kwargs)

        _config_mod.get_settings_or_config = counting
        # Rebind the already-imported references in the hot modules.
        for mod_name in ("nautobot.dcim.models.devices", "nautobot.dcim.models.locations"):
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, "get_settings_or_config"):
                mod.get_settings_or_config = counting
        return self

    def __exit__(self, *exc):
        _config_mod.get_settings_or_config = self._original
        for mod_name in ("nautobot.dcim.models.devices", "nautobot.dcim.models.locations"):
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, "get_settings_or_config"):
                mod.get_settings_or_config = self._original


class QueryCollector:
    """Count and record SQL without Django's query-log ceiling.

    ``CaptureQueriesContext`` reads ``connection.queries``, a deque capped at
    9000 entries; a bulk operation overflows it and the slice arithmetic then
    reports zero. ``execute_wrapper`` has no such limit and works with
    DEBUG=False.
    """

    def __init__(self):
        self.sql = []
        self.total_seconds = 0.0

    def __call__(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.total_seconds += time.perf_counter() - start
            self.sql.append(sql)


def fixtures():
    """Deterministically chosen prerequisite objects, picked once up front."""
    device = Device.objects.order_by("pk").first()
    # The most-loaded device: bulk updates need enough rows to be meaningful.
    busiest = Device.objects.annotate(_n=Count("interfaces")).order_by("-_n", "pk").first()
    return {
        "device": device,
        "busiest_device": busiest,
        "device_type": DeviceType.objects.order_by("pk").first(),
        "location": Location.objects.filter(devices__isnull=False).order_by("pk").first(),
        "device_status": Status.objects.get_for_model(Device).order_by("pk").first(),
        "device_role": Role.objects.get_for_model(Device).order_by("pk").first(),
        "iface_status": Status.objects.get_for_model(Interface).order_by("pk").first(),
        "ip_status": Status.objects.get_for_model(IPAddress).order_by("pk").first(),
        "prefix": Prefix.objects.order_by("pk").first(),
        "namespace": Namespace.objects.order_by("pk").first(),
    }


# --- operations -------------------------------------------------------------
# Each takes the fixture dict and performs one logical write.

def op_create_device(f):
    Device(
        name="perf-probe-device",
        device_type=f["device_type"],
        role=f["device_role"],
        status=f["device_status"],
        location=f["location"],
    ).validated_save()


def op_update_device_name(f):
    d = f["device"]
    d.name = f"{d.name}-perf"
    d.validated_save()


def op_update_device_noop(f):
    """Save with no field changes -- the floor cost of the write path."""
    f["device"].validated_save()


def op_delete_device(f):
    Device.objects.order_by("-pk").first().delete()


def op_create_interface(f):
    Interface(
        device=f["device"], name="perf-probe-0", type="1000base-t", status=f["iface_status"]
    ).validated_save()


def op_create_interfaces_50(f):
    for i in range(50):
        Interface(
            device=f["device"], name=f"perf-probe-{i}", type="1000base-t", status=f["iface_status"]
        ).validated_save()


def op_create_tag(f):
    """A minimal object -- control for how much cost is model-specific."""
    Tag(name="perf-probe-tag").validated_save()


def op_create_ipaddress(f):
    # The address must fall inside a prefix in the *same* namespace, and must
    # not already be taken -- the seeded dataset fills the low host numbers.
    p = f["prefix"]
    taken = set(IPAddress.objects.filter(parent=p).values_list("host", flat=True))
    host = next(
        (str(p.prefix[i]) for i in range(2, min(p.prefix.size - 1, 250))
         if str(p.prefix[i]) not in taken),
        None,
    )
    if host is None:
        raise RuntimeError("no free host address in the chosen prefix")
    IPAddress(address=f"{host}/{p.prefix_length}", status=f["ip_status"],
              namespace=p.namespace).validated_save()


# --- bulk operations --------------------------------------------------------
# The interesting comparison is the triple below. All three create the same 100
# rows, so the deltas isolate what change logging actually costs:
#
#   .loop       per-object save, change logging inline   (what naive code does)
#   .deferred   per-object save, ObjectChanges batched   (what bulk views do)
#   .bulk_create  no signals, no validation, no changelog (the floor)
#
# Note that the deferred path still calls to_objectchange() once per object at
# flush time, so it pays the same double serialization -- just later.

BULK_N = 100


def _bulk_interfaces(f, prefix):
    return [
        Interface(device=f["device"], name=f"{prefix}-{i}", type="1000base-t",
                  status=f["iface_status"])
        for i in range(BULK_N)
    ]


def op_bulk_create_interfaces_loop(f):
    for iface in _bulk_interfaces(f, "perf-loop"):
        iface.validated_save()


def op_bulk_create_interfaces_deferred(f):
    with deferred_change_logging_for_bulk_operation():
        for iface in _bulk_interfaces(f, "perf-defer"):
            iface.validated_save()


def op_bulk_create_interfaces_django(f):
    """Floor only -- skips validation, signals and change logging entirely."""
    Interface.objects.bulk_create(_bulk_interfaces(f, "perf-bulkc"))


def op_bulk_update_interfaces_deferred(f):
    """What a bulk-edit view does (core/views/mixins.py:1427)."""
    qs = list(Interface.objects.filter(device=f["busiest_device"]).order_by("pk")[:BULK_N])
    with deferred_change_logging_for_bulk_operation():
        for iface in qs:
            iface.description = "perf-probe"
            iface.validated_save()


def op_bulk_delete_interfaces(f):
    pks = list(
        Interface.objects.filter(device=f["busiest_device"]).order_by("-pk")
        .values_list("pk", flat=True)[:BULK_N]
    )
    Interface.objects.filter(pk__in=pks).delete()


OPERATIONS = [
    ("create.device", op_create_device),
    ("update.device.name", op_update_device_name),
    ("update.device.noop", op_update_device_noop),
    ("delete.device", op_delete_device),
    ("create.interface", op_create_interface),
    ("create.interfaces.x50", op_create_interfaces_50),
    ("create.ipaddress", op_create_ipaddress),
    ("create.tag", op_create_tag),
    (f"bulk.create.interfaces.x{BULK_N}.loop", op_bulk_create_interfaces_loop),
    (f"bulk.create.interfaces.x{BULK_N}.deferred", op_bulk_create_interfaces_deferred),
    (f"bulk.create.interfaces.x{BULK_N}.bulk_create", op_bulk_create_interfaces_django),
    (f"bulk.update.interfaces.x{BULK_N}.deferred", op_bulk_update_interfaces_deferred),
    (f"bulk.delete.interfaces.x{BULK_N}", op_bulk_delete_interfaces),
]


def measure(user, name, fn, f):
    """Run one operation in a rolled-back transaction, capturing its SQL.

    Note: the ObjectChange bookkeeping count runs inside the capture, so every
    measurement carries one extra SELECT. It is constant across operations and
    across runs, so it cancels in any baseline-vs-current comparison.
    """
    changes_before = ObjectChange.objects.count()
    error = None
    collector = QueryCollector()
    config_counter = ConfigCallCounter()
    with config_counter, connection.execute_wrapper(collector):
        start = time.perf_counter()
        try:
            with transaction.atomic():
                with web_request_context(user, context_detail="perf-tier1w"):
                    fn(f)
                changes_after = ObjectChange.objects.count()
                raise Rollback
        except Rollback:
            pass
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            changes_after = changes_before
        elapsed_ms = (time.perf_counter() - start) * 1000.0

    queries = collector.sql
    shapes = Counter(normalize_sql(q) for q in queries)
    worst_shape, worst_count = (shapes.most_common(1) or [("", 0)])[0]
    return {
        "operation": name,
        "error": error,
        "wall_ms": round(elapsed_ms, 2),
        "query_count": len(queries),
        "db_ms": round(collector.total_seconds * 1000.0, 2),
        "duplicate_queries": sum(c - 1 for c in shapes.values() if c > 1),
        "config_reads": config_counter.count,
        "object_changes": changes_after - changes_before,
        "worst_repeat_count": worst_count,
        "worst_repeat_sql": worst_shape[:400],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--only", help="substring filter on operation name")
    args = ap.parse_args()

    User = get_user_model()
    user = User.objects.filter(username=PERF_USER).first()
    if user is None:
        user = User.objects.create(username=PERF_USER, is_superuser=True, is_staff=True, is_active=True)

    f = fixtures()
    missing = [k for k, v in f.items() if v is None]
    if missing:
        print(f"!! missing fixtures, some operations will fail: {missing}", file=sys.stderr)

    records = []
    for name, fn in OPERATIONS:
        if args.only and args.only not in name:
            continue
        measure(user, name, fn, f)  # warmup
        reps = [measure(user, name, fn, f) for _ in range(max(2, args.reps))]
        rec = reps[-1]
        qcounts = {r["query_count"] for r in reps}
        rec["query_count_stable"] = len(qcounts) == 1
        if len(qcounts) > 1:
            rec["query_count_range"] = [min(qcounts), max(qcounts)]
        records.append(rec)
        flag = f"  <-- {rec['error']}" if rec["error"] else ""
        print(f"{name:34s} q={rec['query_count']:<5} cfg={rec['config_reads']:<6} "
              f"changes={rec['object_changes']:<3} {rec['wall_ms']}ms{flag}", file=sys.stderr)

    records.sort(key=lambda r: r["query_count"], reverse=True)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"schema": 1, "operations": records}, fh, indent=2, sort_keys=True)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
