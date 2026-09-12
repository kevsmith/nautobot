#!/usr/bin/env python
"""Queries per rendered row for UI list views, with the row-rendering request actually issued.

A Nautobot list view builds its table over `queryset.none()` unless the request carries
`HX-Request` (finding 44), so a probe that omits the header measures an empty table. This sends it,
renders 25 rows and then 100, and reports the slope. A table whose count grows with the page has a
per-row read; one that does not is already prefetched.

The read screen is REST-only, so this is the first per-row instrument pointed at the UI surface.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_ui_table_slope.py
"""

import collections
import os
import re
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

PAGES = [int(x) for x in os.environ.get("PERF_PAGE_SIZES", "25,100").split(",")]

URLS = [
    "/dcim/devices/",
    "/dcim/device-types/",
    "/dcim/interfaces/",
    "/dcim/modules/",
    "/dcim/module-types/",
    "/dcim/cables/",
    "/dcim/racks/",
    "/dcim/locations/",
    "/dcim/front-ports/",
    "/dcim/rear-ports/",
    "/ipam/prefixes/",
    "/ipam/ip-addresses/",
    "/ipam/vlans/",
    "/circuits/circuits/",
    "/extras/statuses/",
    "/extras/roles/",
    "/extras/tags/",
    "/extras/object-changes/",
    "/extras/dynamic-groups/",
    "/tenancy/tenants/",
    "/virtualization/virtual-machines/",
]


def normalize(sql):
    sql = re.sub(r"'[^']*'", "?", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    sql = re.sub(r"IN \([^)]*\)", "IN (?)", sql)
    return " ".join(sql.split())[:96]


def measure(client, url, per_page):
    target = f"{url}?per_page={per_page}"
    client.get(target, HTTP_HX_REQUEST="true")  # warm-up, discarded
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(target, HTTP_HX_REQUEST="true")
    rows = resp.content.count(b'<tr class="') or resp.content.count(b"<tr>")
    shapes = collections.Counter(normalize(q["sql"]) for q in ctx.captured_queries)
    return resp.status_code, rows, len(ctx.captured_queries), shapes


def main():
    urls = sys.argv[1:] or URLS
    print(f"  {'url':34s} {'status':>6} {'rows':>12} {'queries':>14}   slope")
    for url in urls:
        points = [measure(client, url, n) for n in PAGES]
        statuses = {p[0] for p in points}
        rows = [p[1] for p in points]
        queries = [p[2] for p in points]
        delta = rows[-1] - rows[0]
        slope = (queries[-1] - queries[0]) / delta if delta else 0.0
        flag = "  <-- SCALES" if slope >= 0.1 else ""
        print(
            f"  {url:34s} {sorted(statuses)[0]:>6} {rows[0]:>5}->{rows[-1]:<6} "
            f"{queries[0]:>6}->{queries[-1]:<7} {slope:6.3f} q/row{flag}"
        )
        if flag and os.environ.get("PERF_SHAPES"):
            for sql, n in points[-1][3].most_common(3):
                print(f"        x{n:<4} {sql}")


client = get_perf_client()

if __name__ == "__main__":
    main()
