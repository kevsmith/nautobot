#!/usr/bin/env python
"""Where do the ~200 queries of a cable create actually go?

Finding 40 measured the cost -- roughly 220 queries and 0.34s per cable, linear
from 25 to 96 per request -- and deliberately stopped there. This attributes it.

The hypothesis on the record is cable-path recomputation: `CablePath.from_origin`
runs from a post-save signal (dcim/signals.py:67, :80) and walks front port to
rear port to the next cable. Two things already argue for it and one against.
For: a cable with both terminations null costs ~45 queries against ~200 for a
connected one, and a path is exactly what a null-terminated cable does not have.
Against: front-port-to-front-port cables -- the patch-panel links whose paths
should be *longest* -- measured cheaper per cable (189) than power cables that
terminate immediately (220). Chain depth therefore is not the story, and this
branch's record is that hypotheses formed by reading code are usually wrong.

So: capture every query with its Python stack, keep only the frames inside
nautobot, and group by call site. The same technique settled findings 30 and 32.

    perf/dc.sh exec -T nautobot python /source/perf/probe_cable_attribution.py
"""

import argparse
from collections import Counter
import json
import os
import sys
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection, transaction  # noqa: E402
from django.test.client import RequestFactory  # noqa: E402
from django.urls import reverse  # noqa: E402
from rest_framework.request import Request  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from payloads import PayloadBuilder  # noqa: E402
from probe_cable_bulk import payloads_from_existing  # noqa: E402
from tier1_queries import get_perf_client, normalize_sql  # noqa: E402
from tier1w_writes import Rollback  # noqa: E402

# Frames from these files are plumbing that every query passes through; showing
# them would bury the call site that chose to issue the query.
UNINTERESTING = ("/core/models/querysets.py", "/core/models/managers.py")


class StackCollector:
    """Count queries by the nautobot call site that issued them."""

    def __init__(self, depth=4):
        self.depth = depth
        self.sites = Counter()
        # Shapes *within* each site. Counting per site and showing only the first
        # SQL seen there reads as "16 of this query" when it means "16 queries
        # from this call site, one of which looked like this" -- which is how the
        # first pass over this data misreported a 4 as a 16.
        self.shapes = {}
        self.total = 0

    def __call__(self, execute, sql, params, many, context):
        self.total += 1
        frames = []
        for frame in traceback.extract_stack():
            path = frame.filename
            if "/nautobot/" not in path or "/perf/" in path or "/site-packages/" in path:
                continue
            if any(part in path for part in UNINTERESTING):
                continue
            frames.append(f"{path.split('/nautobot/', 1)[1]}:{frame.lineno} {frame.name}")
        key = tuple(frames[-self.depth :]) if frames else ("<no nautobot frame>",)
        self.sites[key] += 1
        self.shapes.setdefault(key, Counter())[normalize_sql(sql)[:110]] += 1
        return execute(sql, params, many, context)


def attribute(client, url, payloads, Cable, delete_pks, label, top):
    collector = StackCollector()
    status = None
    with connection.execute_wrapper(collector):
        try:
            with transaction.atomic():
                if delete_pks:
                    Cable.objects.filter(pk__in=delete_pks).delete()
                deleted_at = collector.total
                # The delete cascades and rebuilds paths, so its queries must not be
                # folded into the per-cable rates. Counting them was a real defect:
                # it inflated the ORM status-validation figure from 4 to 18 per cable.
                collector.sites.clear()
                collector.shapes.clear()
                response = client.generic("POST", url, json.dumps(payloads), content_type="application/json")
                status = response.status_code
                raise Rollback
        except Rollback:
            pass

    n = len(payloads)
    if not n:
        print(f"\n=== {label}: no cables of this shape in this dataset, skipped ===")
        return
    created = collector.total - deleted_at
    print(f"\n=== {label}: {n} cables, status {status}, {collector.total} queries total ===")
    print(f"    {deleted_at} are the delete that frees the terminations; {created / n:.0f} per cable created")
    print()
    shown = 0
    for key, count in collector.sites.most_common(top):
        shown += count
        shapes = collector.shapes[key]
        print(f"  {count:>6}  ({count / n:>6.1f}/cable)  {len(shapes)} distinct SQL shape(s) from:")
        for frame in key:
            print(f"          {frame}")
        for shape, shape_count in shapes.most_common(3):
            print(f"            {shape_count:>5} ({shape_count / n:>5.1f}/cable)  {shape[:96]}")
        print()
    print(
        f"  top {top} of {len(collector.sites)} distinct sites cover {shown}/{collector.total} "
        f"queries ({shown / collector.total * 100:.0f}%)"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--depth", type=int, default=4)
    args = ap.parse_args()

    from nautobot.dcim.api.serializers import CableSerializer
    from nautobot.dcim.models import Cable

    client = get_perf_client()
    context = {"request": Request(RequestFactory().post("/api/")), "depth": 0}
    url = reverse("dcim-api:cable-list")

    for label, term_type in (("power cables", None), ("patch-panel cables", "dcim.frontport")):
        payloads, pks = payloads_from_existing(Cable, args.n, term_type)
        attribute(client, url, payloads, Cable, pks, label, args.top)

    # Control: a cable connected to nothing. Whatever is absent here is what the
    # terminations cost.
    builder = PayloadBuilder()
    payloads, _ = builder.build_create(CableSerializer, Cable, context, count=args.n, tag="cable")
    attribute(client, url, payloads, Cable, None, "unterminated cables (control)", args.top)


if __name__ == "__main__":
    main()
