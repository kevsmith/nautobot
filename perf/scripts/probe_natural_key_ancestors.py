#!/usr/bin/env python
"""Would `natural_key()` be cheaper if it used the ancestors() Nautobot already optimized?

`BaseModel.natural_key()` resolves each `parent__parent__name` lookup with a plain
`getattr` walk -- one lazy foreign-key load per level. Meanwhile
`TreeQuerySet.ancestors()` (finding 10) already solves that exact walk: it uses
django-tree-queries' recursive CTE when tree fields are present, and otherwise pulls
ANCESTOR_JOIN_DEPTH levels per query with select_related *and populates the foreign-key
cache on the way*.

So the question is not "should Nautobot use a CTE" -- it has one -- but whether
natural_key() should route through the optimized walk instead of its own.

Because ancestors() warms `_state.fields_cache`, a cheap proxy for the change is:
call ancestors() first, then natural_key(), and count the difference. This measures
the ceiling without touching product code.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_natural_key_ancestors.py
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


class Count_:
    def __init__(self):
        self.n = 0
        self.shapes = Counter()

    def __call__(self, execute, sql, params, many, context):
        self.n += 1
        self.shapes[normalize_sql(sql)[:60]] += 1
        return execute(sql, params, many, context)


def measure(fn):
    c = Count_()
    with connection.execute_wrapper(c):
        fn()
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()

    from nautobot.dcim.models import Location

    print(f"max_depth = {Location.objects.max_depth}")
    print(f"natural_key_field_lookups = {list(Location.natural_key_field_lookups)}\n")

    # Deepest locations first -- the shallow ones cost nothing either way.
    deep = sorted(
        Location.objects.without_tree_fields().all(),
        key=lambda loc: len(list(Location.objects.ancestors(loc))),
        reverse=True,
    )[: args.limit]

    print(f"{'location':34s} {'depth':>5} {'plain':>6} {'via ancestors':>14} {'saved':>6}")
    total_plain = total_warm = 0
    for loc in deep:
        depth = len(list(Location.objects.ancestors(loc)))

        cold = Location.objects.without_tree_fields().get(pk=loc.pk)
        plain = measure(cold.natural_key)

        cold2 = Location.objects.without_tree_fields().get(pk=loc.pk)

        def warmed(obj=cold2):
            list(Location.objects.ancestors(obj))  # warms the FK cache
            obj.natural_key()

        warm = measure(warmed)
        total_plain += plain.n
        total_warm += warm.n
        print(f"{str(loc)[:34]:34s} {depth:>5} {plain.n:>6} {warm.n:>14} {plain.n - warm.n:>6}")

    print(
        f"\ntotals over {len(deep)} deepest locations: plain {total_plain}, "
        f"via ancestors {total_warm} ({(total_warm - total_plain) / max(total_plain, 1) * 100:+.0f}%)"
    )
    print(
        "\nnote: 'via ancestors' includes the ancestors() query itself, so it is the "
        "real cost of the alternative, not just the saving."
    )


if __name__ == "__main__":
    main()
