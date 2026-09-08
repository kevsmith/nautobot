#!/usr/bin/env python
"""How many CablePath rebuilds does one REST cable create pay, and what do they cost?

Queue item 5. `CableSerializer._apply_terminations()` writes CableToCableTermination
rows in a loop; `rebuild_paths_on_join_change` is a post_save/post_delete receiver on
that model and, outside a `defer_cable_path_rebuilds()`, calls `rebuild_paths()` per
row. A two-ended cable therefore pays two rebuilds where the deferral would pay one.

This measures rather than assumes, because the deferral is not free: its flush does
`Cable.objects.filter(pk=cable_id).first()` per dirty cable, so it adds a query while
removing a rebuild. The net is the whole question, and reading the code cannot answer
it -- five hypotheses formed that way on this branch were wrong.

Termination type matters more than it looks, per probe_cable_bulk.py: the first cables
by pk here are power-port to power-outlet, which terminate immediately, while databot's
are front-port to front-port, where the path walks on through rear ports. Different
code paths, so both are reported.

Every POST is rolled back. Nothing is left behind.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_defer_cable_paths.py
"""

import argparse
import json
import os
import sys
import time
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection, transaction  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_cable_bulk import payloads_from_existing  # noqa: E402  (kept for parity)
from tier1_queries import get_perf_client  # noqa: E402
from tier1w_writes import Rollback  # noqa: E402


def cables_by_a_end_fk(Cable, fk, n):
    """N cables whose A-end termination row has `fk` populated, newest-relation-first.

    Selects through the `terminations` relation rather than `termination_a_type`. The
    generic FK still filters but is deprecated, and the campus dataset has zero
    front-port A ends, so a shape can legitimately be absent -- which must not be
    confused with a filter that has stopped working.
    """
    return list(
        Cable.objects.filter(**{f"terminations__{fk}__isnull": False}, terminations__cable_end="A")
        .distinct()
        .order_by("pk")[:n]
    )


def payloads_for(Cable, cables):
    """Recreate create-payloads for the given cables, terminations included."""
    from django.contrib.contenttypes.models import ContentType

    out, pks = [], []
    for cable in cables:
        pks.append(cable.pk)
        payload = {"status": str(cable.status_id), "type": cable.type or None, "label": cable.label or ""}
        for end in ("a", "b"):
            ct_id = getattr(cable, f"termination_{end}_type_id")
            obj_id = getattr(cable, f"termination_{end}_id")
            if ct_id and obj_id:
                ct = ContentType.objects.get(pk=ct_id)
                payload[f"termination_{end}_type"] = f"{ct.app_label}.{ct.model}"
                payload[f"termination_{end}_id"] = str(obj_id)
        out.append({k: v for k, v in payload.items() if v is not None})
    return out, pks


class Buckets:
    """Split queries into those issued inside rebuild_paths() and those outside.

    Depth rather than a boolean: rebuild_paths is re-entrant through the deferred
    flush, and a boolean would under-count the nested case.
    """

    def __init__(self):
        self.depth = 0
        self.inside = 0
        self.outside = 0

    def __call__(self, execute, sql, params, many, context):
        if self.depth > 0:
            self.inside += 1
        else:
            self.outside += 1
        return execute(sql, params, many, context)


def measure(client, url, payloads, Cable, delete_pks):
    from nautobot.dcim import signals

    buckets = Buckets()
    calls = {"n": 0, "ms": 0.0, "each": []}
    real = signals.rebuild_paths

    def counting_rebuild_paths(obj):
        calls["n"] += 1
        buckets.depth += 1
        q0 = buckets.inside
        t = time.perf_counter()
        try:
            return real(obj)
        finally:
            ms = (time.perf_counter() - t) * 1000.0
            calls["ms"] += ms
            calls["each"].append({"queries": buckets.inside - q0, "ms": round(ms, 1), "arg": type(obj).__name__})
            buckets.depth -= 1

    signals.rebuild_paths = counting_rebuild_paths
    status, detail, elapsed = None, "", 0.0
    try:
        with connection.execute_wrapper(buckets):
            start = time.perf_counter()
            try:
                with transaction.atomic():
                    # The terminations are occupied by the cables being recreated, so
                    # those have to go first or every payload fails its occupied check.
                    Cable.objects.filter(pk__in=delete_pks).delete()
                    # Deletes fire the same receiver; only the create is under test.
                    calls["n"], calls["ms"], calls["each"] = 0, 0.0, []
                    buckets.inside, buckets.outside = 0, 0
                    response = client.generic("POST", url, json.dumps(payloads), content_type="application/json")
                    status = response.status_code
                    if status >= 300:
                        detail = response.content[:1500].decode(errors="replace")
                    raise Rollback
            except Rollback:
                pass
            except Exception:
                detail = traceback.format_exc()
            elapsed = (time.perf_counter() - start) * 1000.0
    finally:
        signals.rebuild_paths = real
    return {
        "status": status,
        "rebuild_calls": calls["n"],
        "rebuild_ms": round(calls["ms"], 1),
        "rebuild_each": calls["each"],
        "queries_inside_rebuild": buckets.inside,
        "queries_outside": buckets.outside,
        "queries_total": buckets.inside + buckets.outside,
        "wall_ms": round(elapsed, 1),
        "detail": detail,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1, help="cables per POST")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--shapes", default="interface,power_port",
                    help="comma-separated CableToCableTermination FK names to select A ends by")
    ap.add_argument(
        "--drop-b",
        action="store_true",
        help="post a one-ended cable: the control. One termination row means one rebuild in "
        "both arms, so the deferral can only add its flush re-fetch -- if it shows a small "
        "regression here that is the mechanism working, not a surprise.",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from django.urls import reverse

    from nautobot.dcim.models import Cable

    client = get_perf_client()
    url = reverse("dcim-api:cable-list")
    out = []
    shapes = [(f"{fk} A-end", fk) for fk in args.shapes.split(",") if fk]
    for label, fk in shapes:
        payloads, pks = payloads_for(Cable, cables_by_a_end_fk(Cable, fk, args.n))
        if args.drop_b:
            payloads = [{k: v for k, v in pl.items() if not k.startswith("termination_b")} for pl in payloads]
            label += " one-ended (CONTROL)"
        if not payloads:
            # Say so. A shape that matched nothing must not read as a shape that cost
            # nothing -- that is the write screen's null-termination failure again.
            out.append({"shape": label, "error": "no cables matched"})
            if not args.json:
                print(f"  {label:30s} SKIPPED -- no cables in this dataset match")
            continue
        ends = sum(1 for p in payloads for e in ("a", "b") if f"termination_{e}_id" in p)
        for rep in range(args.reps):
            r = measure(client, url, payloads, Cable, pks)
            r.update({"shape": label, "n": args.n, "termination_ends": ends, "rep": rep + 1})
            out.append(r)
            if not args.json:
                print(
                    f"  {label:30s} rep{rep + 1} status={r['status']} "
                    f"rebuilds={r['rebuild_calls']} ({r['rebuild_ms']}ms) "
                    f"q_total={r['queries_total']} q_in_rebuild={r['queries_inside_rebuild']} "
                    f"wall={r['wall_ms']}ms  ends={ends}"
                )
                for i, c in enumerate(r["rebuild_each"], 1):
                    print(f"      rebuild {i}: {c['queries']:>3} queries, {c['ms']}ms  (arg {c['arg']})")
                if r["detail"]:
                    print(r["detail"][:600])
    if args.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
