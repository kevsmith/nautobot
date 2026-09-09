#!/usr/bin/env python
"""Classify a page's SQL compilations: executed, nested subquery, or genuinely discarded.

`probe_query_compilations.py` reports `compiled - executed` and calls the remainder
"discarded". That difference is the right signal for an A/B -- both arms compile the same
subqueries, so a change in the delta is attributable -- but it is the wrong reading of the
absolute number, and this probe exists because that distinction was initially missed.

A compilation can fail to correspond to an execution for two very different reasons:

  nested      A query containing a subquery calls `as_sql()` on the subquery's compiler
              too. One execution, several compilations, nothing wasted. Nautobot's
              `restrict(user, "view")` builds `__in` subqueries, so list views are full of
              these.

  discarded   `as_sql()` raises `EmptyResultSet`, so the compiled query is thrown away and
              never runs. Iterating `queryset.none()` does this: 604us for Location and
              973us for Device with zero SQL executed, against 7.4us for the clone alone.
              This is real waste.

Only the second is worth removing, so this separates them by tracking compiler nesting
depth and catching `EmptyResultSet`, and attributes each discarded compilation to the
nearest Nautobot frame that caused it -- the same approach `attribute.py` takes for
executed queries.

Counts are deterministic and safe to read on a busy box.

    perf/scripts/dc.sh exec -T nautobot \
        python /source/perf/scripts/probe_discarded_compilations.py [scenario-id ...]
"""

import collections
import os
import sys
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.core.exceptions import EmptyResultSet  # noqa: E402
from django.db import connection  # noqa: E402
from django.db.models.sql.compiler import SQLCompiler  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402
import workload as workload_mod  # noqa: E402

DEFAULT_SCENARIOS = ["ui.device.list", "ui.device.list.rows", "ui.vlan.list", "ui.home"]


def nearest_nautobot_frame():
    for frame in reversed(traceback.extract_stack()):
        path = frame.filename
        if "/nautobot/" in path and "/django/" not in path and "site-packages" not in path:
            return f"{path.split('/nautobot/')[-1]}:{frame.lineno} {frame.name}"
    return "(no nautobot frame)"


class Classifier:
    """Count as_sql calls by nesting depth and by whether they raise EmptyResultSet."""

    def __init__(self):
        self.depth = 0
        self.top_ok = 0
        self.nested_ok = 0
        self.discarded_top = 0
        self.discarded_nested = 0
        self.sites = collections.Counter()
        self.site_models = collections.defaultdict(collections.Counter)
        self._original = SQLCompiler.as_sql

    def __enter__(self):
        c = self
        original = self._original

        def as_sql(compiler_self, *args, **kwargs):
            c.depth += 1
            nested = c.depth > 1
            try:
                result = original(compiler_self, *args, **kwargs)
            except EmptyResultSet:
                if nested:
                    c.discarded_nested += 1
                else:
                    c.discarded_top += 1
                    site = nearest_nautobot_frame()
                    c.sites[site] += 1
                    model = getattr(getattr(compiler_self.query, "model", None), "__name__", "?")
                    c.site_models[site][model] += 1
                raise
            else:
                if nested:
                    c.nested_ok += 1
                else:
                    c.top_ok += 1
                return result
            finally:
                c.depth -= 1

        SQLCompiler.as_sql = as_sql
        return self

    def __exit__(self, *exc):
        SQLCompiler.as_sql = self._original
        return False


class ExecutionCounter:
    def __init__(self):
        self.n = 0

    def __call__(self, execute, sql, params, many, context):
        self.n += 1
        return execute(sql, params, many, context)


def main():
    ids = sys.argv[1:] or DEFAULT_SCENARIOS
    resolved, _ = workload_mod.resolve(workload_mod.DEFAULT_WORKLOAD)
    rows = {r["id"]: r for r in resolved}
    missing = [i for i in ids if i not in rows]
    if missing:
        sys.exit(f"unknown scenario(s): {', '.join(missing)}")

    client = get_perf_client()
    for sid in ids:
        row = rows[sid]
        url, headers = row["url"], row.get("headers") or {}
        client.get(url, headers=headers)  # warm

        execs = ExecutionCounter()
        with Classifier() as c, connection.execute_wrapper(execs):
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"{sid}: NON-200 {resp.status_code}, skipped")
            continue

        total = c.top_ok + c.nested_ok + c.discarded_top + c.discarded_nested
        print(f"\n=== {sid} ===")
        print(f"  SQL executed                     {execs.n:5d}")
        print(f"  as_sql total                     {total:5d}")
        print(f"    top-level, compiled ok         {c.top_ok:5d}")
        print(f"    nested subquery, compiled ok   {c.nested_ok:5d}   (one execution covers these)")
        print(f"    DISCARDED (EmptyResultSet)     {c.discarded_top + c.discarded_nested:5d}"
              f"   top {c.discarded_top}, nested {c.discarded_nested}")
        if c.sites:
            print("  --- discarded, by originating site ---")
            for site, n in c.sites.most_common(10):
                models = ", ".join(f"{m}x{k}" for m, k in c.site_models[site].most_common(3))
                print(f"    {n:5d}  {site}")
                print(f"           {models}")


if __name__ == "__main__":
    main()
