#!/usr/bin/env python
"""Query count for the InterfaceRedundancyGroup detail page, which renders the one table column
that `BaseTable`'s accessor walk drops.

`probe_accessor_walk_gaps.py` finds exactly one column across 183 table classes whose prefetch is
lost to the FK guard: `InterfaceRedundancyGroupAssociationTable.interface__ip_addresses`. That
table has no list view of its own -- it renders on the redundancy group's detail page -- so the
slope probe cannot reach it.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_irg_detail.py
"""

import collections
import os
import re
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import reverse  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

from nautobot.dcim.models import InterfaceRedundancyGroup  # noqa: E402


def normalize(sql):
    sql = re.sub(r"'[^']*'", "?", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    return " ".join(sql.split())[:94]


def main():
    client = get_perf_client()
    groups = list(InterfaceRedundancyGroup.objects.annotate(n=Count("interfaces")).order_by("-n")[:3])
    if not groups:
        sys.exit("no InterfaceRedundancyGroups in this dataset")
    for group in groups:
        url = reverse("dcim:interfaceredundancygroup", kwargs={"pk": group.pk})
        client.get(url)  # warm-up, discarded
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url)
        shapes = collections.Counter(normalize(q["sql"]) for q in ctx.captured_queries)
        n = group.interfaces.count()
        per = len(ctx.captured_queries) / n if n else 0
        print(f"  {group.name[:28]:28s} {n:>3} interfaces  status={resp.status_code}  "
              f"queries={len(ctx.captured_queries):>4} ({per:.3f}/interface)")
        if os.environ.get("PERF_SHAPES"):
            for sql, count in shapes.most_common(3):
                print(f"      x{count:<4} {sql}")


if __name__ == "__main__":
    main()
