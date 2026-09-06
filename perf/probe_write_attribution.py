#!/usr/bin/env python
"""Attribute the queries of a create for any model, by call site.

`screen_writes.py` ranks models by cost per created object; this explains one of
them. Same payload builder, same REST path, every query captured with its Python
stack and grouped by the nautobot call site that issued it, with the SQL shapes
inside each site.

The cable investigation needed a bespoke probe because cables have to be deleted
before they can be recreated. Most models do not, so this generalizes it.

    perf/dc.sh exec -T nautobot python /source/perf/probe_write_attribution.py \\
        --model ipam.ipaddresstointerface --n 10
"""

import argparse
import json
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.apps import apps  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test.client import RequestFactory  # noqa: E402
from django.urls import reverse  # noqa: E402
from rest_framework.request import Request  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from payloads import PayloadBuilder  # noqa: E402
from probe_cable_attribution import StackCollector  # noqa: E402
from screen_writes import api_write_routes, serializer_and_model  # noqa: E402
from tier1_queries import get_perf_client  # noqa: E402
from tier1w_writes import Rollback  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="app_label.modelname")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--shapes", type=int, default=3)
    args = ap.parse_args()

    target = apps.get_model(args.model)
    route = next(
        (
            r
            for r in api_write_routes()
            if r["writable"] and getattr(getattr(r["viewset"], "queryset", None), "model", None) is target
        ),
        None,
    )
    if route is None:
        raise SystemExit(f"no writable REST route found for {args.model}")
    serializer_class, model, why = serializer_and_model(route["viewset"])
    if why:
        raise SystemExit(why)

    lookups = list(getattr(model, "natural_key_field_lookups", []) or [])
    hops = sum(lookup.count("__") for lookup in lookups)
    print(f"{model._meta.label_lower}: {len(lookups)} natural-key lookups, {hops} hops")
    for lookup in lookups:
        print(f"    {lookup}")
    print()

    client = get_perf_client()
    context = {"request": Request(RequestFactory().post("/api/")), "depth": 0}
    builder = PayloadBuilder()
    payloads, _ = builder.build_create(serializer_class, model, context, count=args.n, tag=model._meta.model_name[:12])
    url = reverse(route["view_name"])

    collector = StackCollector()
    status = None
    with connection.execute_wrapper(collector):
        try:
            with transaction.atomic():
                response = client.generic("POST", url, json.dumps(payloads), content_type="application/json")
                status = response.status_code
                raise Rollback
        except Rollback:
            pass

    n = args.n
    print(f"=== POST {n} -> {status}, {collector.total} queries ({collector.total / n:.0f} per object) ===\n")
    shown = 0
    for key, count in collector.sites.most_common(args.top):
        shown += count
        shapes = collector.shapes[key]
        print(f"  {count:>5}  ({count / n:>5.1f}/obj)  {len(shapes)} SQL shape(s) from:")
        for frame in key:
            print(f"         {frame}")
        for shape, shape_count in shapes.most_common(args.shapes):
            print(f"           {shape_count:>5} ({shape_count / n:>5.1f}/obj)  {shape[:92]}")
        print()
    print(
        f"  top {args.top} of {len(collector.sites)} sites cover {shown}/{collector.total} "
        f"({shown / collector.total * 100:.0f}%)"
    )


if __name__ == "__main__":
    main()
