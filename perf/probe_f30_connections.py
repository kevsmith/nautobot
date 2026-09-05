#!/usr/bin/env python
"""Query count, scaling and response identity for the three connection endpoints.

Finding 30: /api/dcim/interface-connections/ read 9 fixed queries plus 14 per returned object,
exactly linear against page size. This measures that fit directly rather than inferring it, and
hashes the response body so the two arms can be compared for byte identity -- a prefetch that
changes the payload is a bug, not an optimization.

powerconnections and consoleconnections are measured alongside as controls: they are separate
viewsets that this experiment does not touch, so their numbers must not move.

    perf/dc.sh exec -T nautobot python /source/perf/probe_f30_connections.py
"""

import hashlib
import json
import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

ENDPOINTS = [
    ("interface-connections", "/api/dcim/interface-connections/"),
    # Controls. power- and console-connections are separate viewsets this experiment does not
    # touch. The UI list view shares `CablePath.interface_connections()` with the API viewset, so
    # it is the one place a change to that classmethod could reach something unintended.
    ("power-connections", "/api/dcim/power-connections/"),
    ("console-connections", "/api/dcim/console-connections/"),
    ("ui.interface-connections", "/dcim/interface-connections/"),
]
PAGE_SIZES = [int(x) for x in os.environ.get("PERF_PAGE_SIZES", "5,25,50").split(",")]
REPS = int(os.environ.get("PERF_REPS", "5"))


class Counter:
    """DEBUG is False on the perf stack, so connection.queries is always empty. Count with an
    execute_wrapper, which records regardless of DEBUG."""

    def __init__(self):
        self.n = 0

    def __call__(self, execute, sql, params, many, context):
        self.n += 1
        return execute(sql, params, many, context)


def run(url, limit):
    target = f"{url}?limit={limit}"
    client.get(target)  # warm
    counter = Counter()
    with connection.execute_wrapper(counter):
        resp = client.get(target)
    queries = counter.n
    body = resp.content
    try:
        count = len(json.loads(body).get("results", []))
    except ValueError:
        count = limit  # an HTML list view; the page size is what was asked for
    timings = []
    for _ in range(REPS):
        start = time.perf_counter()
        client.get(target)
        timings.append((time.perf_counter() - start) * 1000.0)
    timings.sort()
    return queries, count, hashlib.sha256(body).hexdigest()[:16], timings[len(timings) // 2]


client = get_perf_client()
print(f"{'endpoint':24s} {'limit':>6} {'objects':>8} {'queries':>8} {'q/obj':>7} {'median':>9}  body sha256")
for name, url in ENDPOINTS:
    for limit in PAGE_SIZES:
        queries, count, digest, median = run(url, limit)
        per_obj = queries / count if count else float("nan")
        print(f"{name:24s} {limit:6d} {count:8d} {queries:8d} {per_obj:7.2f} {median:8.1f}ms  {digest}")
