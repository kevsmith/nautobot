#!/usr/bin/env python
"""Capture the normalised SQL shapes a UI list view emits, per scenario, per arm.

Written to attribute the queries upstream #9501 added to every list view
(finding 88). Tier 1 records `distinct_shapes` as a count, which is enough to
say a shape was added and not enough to say WHICH -- an inference from that
count was wrong once already, because the shape the count suggested had been
present all along under a different queryset.

Replays workload scenarios through the same path tier1_queries.py uses -- same
resolver, same headers, same discarded warmup -- and records every shape with
its repeat count, so "a new shape" and "an existing shape now running twice"
are distinguishable.

Run inside the nautobot container, once per arm:

    python /source/perf/scripts/probe_exportbtn.py --out /source/perf/results/shapes-<arm>.json
"""

import argparse
from collections import Counter
import json
import os
import re
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test.client import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
import workload as workload_mod  # noqa: E402

# Normalisation is imported, not copied. A copy is how the cursor-name gap in
# finding 88 came to exist in six places at once.
from tier1_queries import normalize_sql as normalize  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="ui.", help="substring filter on scenario id")
    ap.add_argument("--raw", help="also dump unnormalised SQL for scenarios matching this substring")
    args = ap.parse_args()

    endpoints, problems = workload_mod.resolve(workload_mod.DEFAULT_WORKLOAD)
    if problems:
        print(f"!! {len(problems)} scenario(s) failed to resolve", file=sys.stderr)

    user = get_user_model().objects.get(username="perfbot")
    # DEBUG=False means ALLOWED_HOSTS is enforced and the test client's default Host
    # of 'testserver' is rejected with a 400 -- which a probe reads as "1 query, 1
    # shape" rather than as a failure. ALLOWED_HOSTS carries '.localhost'.
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)

    out = {}
    for sc in endpoints:
        sid = sc["id"]
        if args.only not in sid:
            continue
        url, headers = sc["url"], sc.get("headers") or {}
        # Content-type and permission caches are process-level and cold only once,
        # so without a discarded warmup the first scenario measured carries shapes
        # no later one does.
        client.get(url, follow=False, headers=headers)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url, follow=False, headers=headers)
        shapes = Counter(normalize(q["sql"]) for q in ctx.captured_queries)
        entry = {"status": resp.status_code, "total": sum(shapes.values()), "shapes": dict(shapes)}
        if args.raw and args.raw in sid:
            entry["raw"] = [q["sql"] for q in ctx.captured_queries]
        out[sid] = entry
        if resp.status_code != 200:
            print(f"  !! {sid} returned {resp.status_code} -- shapes below describe an error page")
        print(f"  {sid:38} status={resp.status_code} queries={entry['total']:4} shapes={len(shapes):3}")

    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"wrote {args.out}")


main()
