#!/usr/bin/env python
"""Attribute one scenario's queries to the Python call sites that issue them.

Five hypotheses on this branch formed by reading code turned out to be wrong,
and every correction came from instrumentation. This is that instrumentation:
group a scenario's queries by table and by the nearest Nautobot frame that
issued them, so a hypothesis is explained by a measurement rather than
predicted by one.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/attribute.py <scenario-id>
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

headers = {}
if len(sys.argv) > 2 and sys.argv[1] == "--url":
    scenario = target = sys.argv[2]
else:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "api.interface.depth1"
    resolved, _ = workload_mod.resolve(workload_mod.DEFAULT_WORKLOAD)
    rows = {r["id"]: r for r in resolved}
    if scenario not in rows:
        sys.exit(f"unknown scenario {scenario}; use --url <path> for anything outside workload.yml")
    target = rows[scenario]["url"]
    # `headers` is not decoration. Nautobot's UI defers row rendering to a second
    # HTMX request, so `ui.device.list` and `ui.device.list.rows` share a URL and
    # differ only by `HX-Request`. Dropping the header made every `.rows`
    # attribution silently describe the chrome page: both scenarios reported the
    # same 9 queries and no dcim_device query at all, against a table that
    # renders 100 rows. tier1_queries.py has always passed them; this did not.
    headers = rows[scenario].get("headers") or {}

# Frames that are always plumbing rather than a call site. Kept deliberately short: a
# module belongs here only if it appears between an arbitrary caller and the ORM.
PLUMBING = (
    "nautobot/core/models/querysets.py",
    "nautobot/core/models/managers.py",
)

client = get_perf_client()
client.get(target, headers=headers)  # warm caches so cold-start work is not attributed

by_table = collections.Counter()
by_site = collections.Counter()
site_tables = collections.defaultdict(collections.Counter)


def wrapper(execute, sql, params, many, context):
    m = re.search(r'FROM "(\w+)"', sql or "")
    table = m.group(1) if m else "?"
    by_table[table] += 1
    # The nearest Nautobot frame, skipping Django and site-packages: the ORM
    # frame that ran the query is never the interesting one.
    #
    # PLUMBING is skipped for the same reason. `RestrictedQuerySet` overrides
    # `_fetch_all`, `exists`, `iterator` and `count` (finding 55), so those frames sit
    # between every caller and Django and would otherwise absorb the attribution: a home
    # page whose 79 queries come from ~34 distinct call sites reported 34 of them as
    # `querysets.py:170 count`, which names the plumbing rather than the cause. The point
    # of this instrument is the caller.
    for frame in reversed(traceback.extract_stack()):
        path = frame.filename
        if any(p in path for p in PLUMBING):
            continue
        if "/nautobot/" in path and "/django/" not in path and "site-packages" not in path:
            site = f"{path.split('/nautobot/')[-1]}:{frame.lineno} {frame.name}"
            by_site[site] += 1
            site_tables[site][table] += 1
            break
    return execute(sql, params, many, context)


with connection.execute_wrapper(wrapper):
    resp = client.get(target, headers=headers)

hdr = f", headers {headers}" if headers else ""
print(f"{scenario}: status {resp.status_code}, {sum(by_table.values())} queries{hdr}\n")
print("--- by table ---")
for table, n in by_table.most_common(12):
    print(f"  {n:5d}  {table}")
print("\n--- by originating call site ---")
for site, n in by_site.most_common(12):
    tables = ", ".join(f"{t}x{c}" for t, c in site_tables[site].most_common(3))
    print(f"  {n:5d}  {site}\n         {tables}")
