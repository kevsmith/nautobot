#!/usr/bin/env python
"""Query count, wall clock and response digest for arbitrary endpoints.

The finding-specific probes (probe_f30_connections.py, probe_f31_*.py) each
hardcode their endpoint list. This one takes URLs on the command line so an A/B
does not need a new probe per experiment.

Prints one row per (url, limit): the query count, the median of REPS timed
requests, and a digest of the response body. The digest is the correctness
control -- a prefetch that changes the payload is a bug, not an optimization --
and the query count is the proof the two arms actually differ, without which the
measurement is void.

    perf/dc.sh exec -T -e PERF_REPS=9 nautobot python /source/perf/probe_endpoint_ab.py \
        "/api/dcim/cables-to-cable-terminations/?depth=1" "/api/dcim/interface-connections/"
"""

import hashlib
import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

REPS = int(os.environ.get("PERF_REPS", "9"))
PAGE_SIZES = [int(x) for x in os.environ.get("PERF_PAGE_SIZES", "25,50").split(",")]


class Counter:
    """DEBUG is False on the perf stack, so connection.queries stays empty. An execute_wrapper
    counts regardless of DEBUG."""

    def __init__(self):
        self.n = 0

    def __call__(self, execute, sql, params, many, context):
        self.n += 1
        return execute(sql, params, many, context)


client = get_perf_client()
print(f"{'url':52s} {'limit':>5} {'queries':>8} {'median':>9}  body sha256")
for url in sys.argv[1:]:
    joiner = "&" if "?" in url else "?"
    for limit in PAGE_SIZES:
        target = f"{url}{joiner}limit={limit}"
        client.get(target)  # warm; the first request after a fresh process is not representative
        counter = Counter()
        with connection.execute_wrapper(counter):
            resp = client.get(target)
        if resp.status_code != 200:
            sys.exit(f"NON-200 {resp.status_code} for {target} -- measurement void")
        timings = []
        for _ in range(REPS):
            start = time.perf_counter()
            client.get(target)
            timings.append((time.perf_counter() - start) * 1000.0)
        timings.sort()
        digest = hashlib.sha256(resp.content).hexdigest()[:16]
        print(f"{url[:52]:52s} {limit:5d} {counter.n:8d} {timings[len(timings) // 2]:8.1f}ms  {digest}")
