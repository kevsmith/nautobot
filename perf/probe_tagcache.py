#!/usr/bin/env python
"""Re-derive finding 31's claim: serialize_object's `_tags` cache never fires.

    tags = getattr(obj, "_tags", []) or obj.tags.only("name")

`[] or X` evaluates X, so an object whose cached tag list is empty queries
anyway. The claim on record is 40 queries for 20 Interfaces as-is, 40 with
`_tags = []`, and 20 with a truthy `_tags`. Counted here rather than believed.

    perf/dc.sh exec -T nautobot python /source/perf/probe_tagcache.py
"""

import os
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402

from nautobot.core.models.utils import serialize_object  # noqa: E402
from nautobot.dcim.models import Interface  # noqa: E402
from nautobot.extras.models import Tag, TaggedItem  # noqa: E402

N = 20


def arm(label, mutate):
    objs = list(Interface.objects.all()[:N])
    for obj in objs:
        mutate(obj)
    connection.queries_log.clear()
    start = time.perf_counter()
    for obj in objs:
        serialize_object(obj)
    elapsed = (time.perf_counter() - start) * 1000.0
    queries = len(connection.queries)
    tables = {}
    for entry in connection.queries:
        sql = entry["sql"]
        for candidate in ("extras_tag", "ipam_vlan", "dcim_interface"):
            if candidate in sql:
                tables[candidate] = tables.get(candidate, 0) + 1
                break
    print(f"{label:34s} {queries:4d} queries  {elapsed:7.2f}ms  {elapsed / N:5.2f}ms/obj  {tables}")
    return queries


assert connection.queries_log.maxlen is None or connection.queries_log.maxlen >= 200, "query log too small"

print(f"serialize_object over {N} Interfaces\n")
as_is = arm("as-is (no _tags attribute)", lambda o: None)
empty = arm("_tags = [] (falsy, defeated)", lambda o: setattr(o, "_tags", []))
truthy = arm("_tags = [Tag(name=...)] (truthy)", lambda o: setattr(o, "_tags", [Tag(name="probe")]))

print(
    f"\nas-is {as_is}, _tags=[] {empty}, truthy {truthy}"
    f"  ->  the empty arm {'does NOT' if empty == as_is else 'DOES'} avoid the query"
)

tagged = TaggedItem.objects.count()
print(f"\nTaggedItem rows in the whole dataset: {tagged}")
