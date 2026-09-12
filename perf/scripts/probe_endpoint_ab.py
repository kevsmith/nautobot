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

    perf/scripts/dc.sh exec -T -e PERF_REPS=9 nautobot python /source/perf/scripts/probe_endpoint_ab.py \
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
# A Nautobot list view builds its table over queryset.none() unless the request carries HX-Request
# (finding 44), so timing a UI list without this header measures a page that rendered no rows. Set
# PERF_HX=1 for UI list scenarios; it is inert for REST endpoints.
EXTRA = {"HTTP_HX_REQUEST": "true"} if os.environ.get("PERF_HX") else {}
# REST paginates on `limit`, the UI on `per_page`, and they are not interchangeable: a UI list asked
# for `?limit=100` WITH the HX header returns a 2-row, 9KB fragment rather than an error, so a probe
# that used the REST parameter would time almost nothing and report it as a page.
PAGE_PARAM = "per_page" if os.environ.get("PERF_HX") else "limit"
# One consequence worth knowing: the parameter is chosen once for the whole run, so UI and REST URLs
# cannot be mixed in a single PERF_HX invocation -- the REST endpoints answer 400 to `per_page`, and
# this probe voids the measurement rather than timing an error page.
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
print(f"{'url':52s} {PAGE_PARAM:>8} {'queries':>8} {'median':>9}  body sha256")
for url in sys.argv[1:]:
    joiner = "&" if "?" in url else "?"
    for limit in PAGE_SIZES:
        target = f"{url}{joiner}{PAGE_PARAM}={limit}"
        client.get(target, **EXTRA)  # warm; the first request after a fresh process is not representative
        counter = Counter()
        with connection.execute_wrapper(counter):
            resp = client.get(target, **EXTRA)
        if resp.status_code != 200:
            sys.exit(f"NON-200 {resp.status_code} for {target} -- measurement void")
        timings = []
        for _ in range(REPS):
            start = time.perf_counter()
            client.get(target, **EXTRA)
            timings.append((time.perf_counter() - start) * 1000.0)
        timings.sort()
        digest = hashlib.sha256(resp.content).hexdigest()[:16]
        print(f"{url[:52]:52s} {limit:8d} {counter.n:8d} {timings[len(timings) // 2]:8.1f}ms  {digest}")
