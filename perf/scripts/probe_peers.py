#!/usr/bin/env python
"""Cost out a batched prefetch for cable peers before writing one.

Finding 26: api.interface.depth1 spends 83 queries in _first_cable_path and 83
in _destination, on peer terminations that are not page members and so never
receive the viewset's prefetches. Three things to establish before changing
anything:

  1. are the peers already in memory, or does reaching them cost queries?
  2. what does one batched prefetch_related_objects over all of them cost?
  3. how many peers are there, and how many are already page members?
"""

import os
import re

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection, models  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from nautobot.dcim.models import Interface  # noqa: E402


def count(label, fn):
    with CaptureQueriesContext(connection) as ctx:
        result = fn()
    tables = {}
    for q in ctx.captured_queries:
        m = re.search(r'FROM "(\w+)"', q["sql"])
        t = m.group(1) if m else "?"
        tables[t] = tables.get(t, 0) + 1
    top = ", ".join(f"{t}x{n}" for t, n in sorted(tables.items(), key=lambda kv: -kv[1])[:4])
    print(f"  {label:48s} {len(ctx.captured_queries):4d} queries   {top}")
    return result


qs = Interface.objects.prefetch_related(
    *Interface.connection_prefetch_related_fields(), *Interface.cable_peer_prefetch_related_fields()
)
page = list(qs[:100])
print(f"page of {len(page)} interfaces\n")

print("1. reaching the peers")
peers = count("get_cable_peer() on all 100", lambda: [i.get_cable_peer() for i in page])
peers = [p for p in peers if p is not None]
page_pks = {(i._meta.concrete_model, i.pk) for i in page}
on_page = sum(1 for p in peers if (p._meta.concrete_model, p.pk) in page_pks)
print(f"   {len(peers)} peers, {on_page} of them already page members, {len(peers) - on_page} not")

print("\n2. what the serializer does today, per peer")
count(
    "cable_paths.first() + .destination on each peer",
    lambda: [(lambda p: p and p.destination)(x.cable_paths.first()) for x in peers],
)

print("\n3. one batched prefetch over the peers instead")
page2 = list(qs[:100])
peers2 = [i.get_cable_peer() for i in page2]
peers2 = [p for p in peers2 if p is not None]
by_model = {}
for p in peers2:
    by_model.setdefault(p._meta.concrete_model, []).append(p)


def batched():
    for model, group in by_model.items():
        models.prefetch_related_objects(group, "cable_paths__destination")


count(f"prefetch_related_objects over {len(peers2)} peers ({len(by_model)} model group(s))", batched)
count(
    "cable_paths.first() + .destination after prefetch",
    lambda: [(lambda p: p and p.destination)(x.cable_paths.first()) for x in peers2],
)
