#!/usr/bin/env python
"""Why does creating one cable run full_clean() so many times?

The attribution probe found 16 `SELECT 1 FROM dcim_cable ... LIMIT 1` per cable
created -- Django's ForeignKey.validate() proving the cable exists, from
full_clean() inside the per-termination serializer in _apply_terminations. Two
terminations should account for two of those, or four if saving re-cleans. It is
16, and neither the code nor the stack explains the factor.

So count the calls rather than reason about them: wrap Model.full_clean and
Model.save, tally by model, and print what one cable create actually does.

    perf/dc.sh exec -T nautobot python /source/perf/probe_cable_clean.py
"""

import argparse
from collections import Counter
import json
import os
import sys
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import models, transaction  # noqa: E402
from django.urls import reverse  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_cable_bulk import payloads_from_existing  # noqa: E402
from tier1_queries import get_perf_client  # noqa: E402
from tier1w_writes import Rollback  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1)
    args = ap.parse_args()

    from nautobot.dcim.models import Cable

    client = get_perf_client()
    url = reverse("dcim-api:cable-list")
    payloads, pks = payloads_from_existing(Cable, args.n)

    cleans, saves = Counter(), Counter()
    real_clean, real_save = models.Model.full_clean, models.Model.save

    def counting_clean(self, *a, **kw):
        cleans[type(self).__name__] += 1
        return real_clean(self, *a, **kw)

    def counting_save(self, *a, **kw):
        saves[type(self).__name__] += 1
        return real_save(self, *a, **kw)

    # The attribution probe grouped by nautobot frames only, which can merge
    # distinct Django-internal paths under one signature. Print the complete
    # stack for the first few of the query in question, so the caller is a fact
    # rather than an inference.
    from django.db import connection

    matches = {"n": 0}

    def explain(execute, sql, params, many, context):
        if 'FROM "dcim_cable" WHERE' in sql and matches["n"] < 2:
            matches["n"] += 1
            print(f"--- match {matches['n']}: {sql[:90]}")
            for line in traceback.format_stack()[:-1]:
                text = line.strip().splitlines()[0]
                if "/perf/" in text:
                    continue
                print(f"      {text}")
            print()
        return execute(sql, params, many, context)

    models.Model.full_clean = counting_clean
    models.Model.save = counting_save
    try:
        with transaction.atomic():
            Cable.objects.filter(pk__in=pks).delete()
            cleans.clear()
            saves.clear()
            with connection.execute_wrapper(explain):
                response = client.generic("POST", url, json.dumps(payloads), content_type="application/json")
            status = response.status_code
            raise Rollback
    except Rollback:
        pass
    finally:
        models.Model.full_clean, models.Model.save = real_clean, real_save

    print(f"POST {args.n} cable(s) -> {status}\n")
    print(f"{'model':44s} {'full_clean()':>13} {'save()':>8}  per cable")
    for name in sorted(set(cleans) | set(saves), key=lambda k: -cleans[k]):
        print(f"{name:44s} {cleans[name]:>13} {saves[name]:>8}  {cleans[name] / args.n:>6.1f} cleans")
    print(f"\ntotal full_clean() calls: {sum(cleans.values())} ({sum(cleans.values()) / args.n:.1f} per cable)")


if __name__ == "__main__":
    main()
