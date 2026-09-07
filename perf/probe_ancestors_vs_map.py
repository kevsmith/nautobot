#!/usr/bin/env python
"""On a page of objects, is routing natural_key() through ancestors() better than the map?

For a single object the optimized ancestor walk wins: a depth-4 Location's natural
key costs 4 queries plain and 2 via `TreeQuerySet.ancestors()`, including the
ancestors() call. But the whole-table map (finding 14) is one query for the *entire
table*, not one per object -- `dcim_location 257 -> 0` on a page of 100 interfaces.

Those scale differently, so the page is where the comparison is decided. Three arms
over the same page:

  map        the map as shipped
  plain      map disabled -- the getattr walk, one lazy load per hop
  ancestors  map disabled, but every distinct Location on the page pre-walked with
             ancestors(), which warms each instance's foreign-key cache

The third is a proxy for routing natural_key() through ancestors() without changing
product code. It flatters that option slightly, because it batches the pre-walk by
distinct location; a real implementation inside natural_key() could not.

    perf/dc.sh exec -T nautobot python /source/perf/probe_ancestors_vs_map.py --limit 100
"""

import argparse
from collections import Counter
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import normalize_sql  # noqa: E402


class Tally:
    def __init__(self):
        self.n = 0
        self.tables = Counter()

    def __call__(self, execute, sql, params, many, context):
        self.n += 1
        shape = normalize_sql(sql)
        for table in ("dcim_location", "dcim_device", "tenancy_tenant"):
            if f'"{table}"' in shape:
                self.tables[table] += 1
                break
        return execute(sql, params, many, context)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    from nautobot.dcim.models import Interface, Location

    def page():
        return list(Interface.objects.order_by("pk")[: args.limit])

    def run(label, enabled, prewalk):
        Location.natural_key_map_enabled = enabled
        objs = page()  # fetched outside the measurement
        tally = Tally()
        with connection.execute_wrapper(tally):
            if prewalk:
                seen = {}
                for obj in objs:
                    loc = obj.device.location if obj.device_id else None
                    if loc is not None and loc.pk not in seen:
                        list(Location.objects.ancestors(loc))
                        seen[loc.pk] = loc
            for obj in objs:
                obj.natural_key()
        print(
            f"{label:34s} {tally.n:>7} {tally.tables['dcim_location']:>10} "
            f"{tally.tables['dcim_device']:>9} {tally.tables['tenancy_tenant']:>8}"
        )
        return tally.n

    original = Location.natural_key_map_enabled
    print(f"page of {args.limit} interfaces, natural_key() for each\n")
    print(f"{'arm':34s} {'queries':>7} {'location':>10} {'device':>9} {'tenant':>8}")
    try:
        run("map (as shipped)", True, False)
        run("plain getattr walk", False, False)
        run("ancestors() pre-walk", False, True)
    finally:
        Location.natural_key_map_enabled = original


if __name__ == "__main__":
    main()
