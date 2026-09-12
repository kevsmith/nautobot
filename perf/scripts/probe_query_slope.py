#!/usr/bin/env python
"""Queries per rendered row, for endpoints suspected of an N+1.

The question an N+1 claim has to answer is not "how many queries" but "how many *more*
queries per additional row". A count that falls could be many things; a count that grows
with the page is a per-object read, and the slope names its size. Reports rows returned at
each page size too, because on this dataset an endpoint with zero rows cannot demonstrate
anything (see perf/dataset-gaps.md).

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_query_slope.py \
        "/api/dcim/front-ports/" "/api/extras/jobs/"
"""

import collections
import json
import os
import re
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

PAGE_SIZES = [int(x) for x in os.environ.get("PERF_PAGE_SIZES", "25,100").split(",")]


def normalize(sql):
    sql = re.sub(r"'[^']*'", "?", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    sql = re.sub(r"IN \([^)]*\)", "IN (?)", sql)
    return " ".join(sql.split())[:104]


def measure(client, url, limit):
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}limit={limit}"
    client.get(full)  # warm-up, discarded
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(full)
    rows = None
    if resp.status_code == 200 and resp["Content-Type"].startswith("application/json"):
        body = json.loads(resp.content)
        rows = len(body.get("results", [])) if isinstance(body, dict) else None
    shapes = collections.Counter(normalize(q["sql"]) for q in ctx.captured_queries)
    return resp.status_code, rows, len(ctx.captured_queries), shapes


def main():
    client = get_perf_client()
    for url in sys.argv[1:]:
        points = [measure(client, url, limit) for limit in PAGE_SIZES]
        statuses = {p[0] for p in points}
        rows = [p[1] for p in points]
        queries = [p[2] for p in points]
        delta_rows = (rows[-1] or 0) - (rows[0] or 0)
        slope = (queries[-1] - queries[0]) / delta_rows if delta_rows else 0.0
        pairs = "  ".join(f"{limit}: {r} rows/{q} q" for limit, (_, r, q, _s) in zip(PAGE_SIZES, points))
        print(f"  {url:58s} status={sorted(statuses)}  {pairs}   slope={slope:.3f} q/row")
        if os.environ.get("PERF_SHAPES") and slope >= 0.1:
            for sql, n in points[-1][3].most_common(4):
                print(f"      x{n:<4} {sql}")


if __name__ == "__main__":
    main()
