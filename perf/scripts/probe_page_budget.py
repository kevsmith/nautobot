#!/usr/bin/env python
"""Where does a page's wall clock actually go? CPU, blocked, or database?

`ui.device.list` reads 9 queries / 6ms db / 286ms wall in the committed Tier 1
baseline, and finding 44 established that the document renders its table over
`queryset.none()` -- so 280ms buys chrome plus list machinery over zero rows. That
280ms has only ever been an inference from subtracting db from wall. Nobody has
checked whether the process was even running for it.

This separates three things a wall-clock number conflates:

  on-CPU        getrusage utime + stime, split so a syscall-heavy request shows up
  off-CPU       wall - cpu; time the process was not running at all
  blocked       voluntary context switches, one per block on I/O

If cpu is close to wall, the page is burning Python and the next question is which
Python -- template against view code. If it is not, something is waiting, and the
count of voluntary context switches says how many times.

Also counts Constance reads, because this branch has an endpoint that made 1,938
Redis round-trips against 977 SQL queries, and SQL-shaped tooling cannot see that.

One warm-up rep per scenario is discarded: process caches are cold on the first
pass and inflate everything.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_page_budget.py
"""

import argparse
import json
import os
import resource
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402
from tier1w_writes import ConfigCallCounter, QueryCollector  # noqa: E402

SCENARIOS = [
    ("ui.chrome.404", "/dcim/no-such-page-chrome-control/", {}),
    ("ui.search", "/search/?q=sw", {}),
    ("ui.device.list", "/dcim/devices/", {}),
    ("ui.device.list.rows", "/dcim/devices/", {"HTTP_HX_REQUEST": "true"}),
    ("ui.device.detail", "/dcim/devices/01782bda-f2d4-401e-9808-3ba3375872ee/", {}),
]


def one(client, url, extra):
    collector = QueryCollector()
    r0 = resource.getrusage(resource.RUSAGE_SELF)
    with ConfigCallCounter() as cfg:
        with connection.execute_wrapper(collector):
            t0 = time.perf_counter()
            resp = client.get(url, **extra)
            wall = (time.perf_counter() - t0) * 1000.0
    r1 = resource.getrusage(resource.RUSAGE_SELF)
    utime = (r1.ru_utime - r0.ru_utime) * 1000.0
    stime = (r1.ru_stime - r0.ru_stime) * 1000.0
    db = collector.total_seconds * 1000.0
    return {
        "status": resp.status_code,
        "bytes": len(resp.content),
        "queries": len(collector.sql),
        "wall_ms": round(wall, 1),
        "cpu_ms": round(utime + stime, 1),
        "utime_ms": round(utime, 1),
        "stime_ms": round(stime, 1),
        "db_ms": round(db, 1),
        "offcpu_ms": round(wall - (utime + stime), 1),
        # wall - cpu - db: off-CPU time the database does not account for.
        "unexplained_ms": round(wall - (utime + stime) - db, 1),
        "vol_ctxsw": r1.ru_nvcsw - r0.ru_nvcsw,
        "invol_ctxsw": r1.ru_nivcsw - r0.ru_nivcsw,
        "minflt": r1.ru_minflt - r0.ru_minflt,
        "majflt": r1.ru_majflt - r0.ru_majflt,
        "config_reads": cfg.count,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_perf_client()
    out = []
    for name, url, extra in SCENARIOS:
        runs = [one(client, url, extra) for _ in range(args.reps + 1)][1:]  # discard warm-up
        med = {k: (statistics.median([r[k] for r in runs]) if isinstance(runs[0][k], (int, float)) else runs[0][k])
               for k in runs[0]}
        med["id"] = name
        med["reps"] = len(runs)
        out.append(med)
        if not args.json:
            print(
                f"  {name:22s} status={med['status']} q={med['queries']:>3} "
                f"wall={med['wall_ms']:>7.1f} cpu={med['cpu_ms']:>7.1f} "
                f"(u={med['utime_ms']:>6.1f} s={med['stime_ms']:>5.1f}) "
                f"db={med['db_ms']:>6.1f} offcpu={med['offcpu_ms']:>6.1f} "
                f"unexpl={med['unexplained_ms']:>6.1f} vcsw={med['vol_ctxsw']:>5} "
                f"icsw={med['invol_ctxsw']:>4} cfg={med['config_reads']:>4}"
            )
    if args.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
