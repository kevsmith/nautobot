#!/usr/bin/env python
"""Attribute one scenario's queries to the Python call sites that issue them.

Five hypotheses on this branch formed by reading code turned out to be wrong,
and every correction came from instrumentation. This is that instrumentation:
group a scenario's queries by table and by the nearest Nautobot frame that
issued them, so a hypothesis is explained by a measurement rather than
predicted by one.

    perf/dc.sh exec -T nautobot python /source/perf/attribute.py <scenario-id>
"""

import collections
import os
import re
import sys
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from django.db import connection  # noqa: E402
from tier1_queries import get_perf_client  # noqa: E402
import workload as workload_mod  # noqa: E402

scenario = sys.argv[1] if len(sys.argv) > 1 else "api.interface.depth1"
resolved, _ = workload_mod.resolve(os.path.join(os.path.dirname(os.path.abspath(__file__)), "workload.yml"))
urls = {r["id"]: r["url"] for r in resolved}
if scenario not in urls:
    sys.exit(f"unknown scenario {scenario}")

client = get_perf_client()
client.get(urls[scenario])  # warm caches so cold-start work is not attributed

by_table = collections.Counter()
by_site = collections.Counter()
site_tables = collections.defaultdict(collections.Counter)


def wrapper(execute, sql, params, many, context):
    m = re.search(r'FROM "(\w+)"', sql or "")
    table = m.group(1) if m else "?"
    by_table[table] += 1
    # The nearest Nautobot frame, skipping Django and site-packages: the ORM
    # frame that ran the query is never the interesting one.
    for frame in reversed(traceback.extract_stack()):
        path = frame.filename
        if "/nautobot/" in path and "/django/" not in path and "site-packages" not in path:
            site = f"{path.split('/nautobot/')[-1]}:{frame.lineno} {frame.name}"
            by_site[site] += 1
            site_tables[site][table] += 1
            break
    return execute(sql, params, many, context)


with connection.execute_wrapper(wrapper):
    resp = client.get(urls[scenario])

print(f"{scenario}: status {resp.status_code}, {sum(by_table.values())} queries\n")
print("--- by table ---")
for table, n in by_table.most_common(12):
    print(f"  {n:5d}  {table}")
print("\n--- by originating call site ---")
for site, n in by_site.most_common(12):
    tables = ", ".join(f"{t}x{c}" for t, c in site_tables[site].most_common(3))
    print(f"  {n:5d}  {site}\n         {tables}")
