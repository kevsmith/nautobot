#!/usr/bin/env python
"""The UI interface-connections view, measured alone in a fresh process.

In the power/console prefetch A/B this view -- a control the change does not
touch -- read +73% in the patched arm with an identical query count, and did so
consistently across three alternating rounds. Consistency is not causation: the
probe measures it LAST, after three API endpoints whose cost differs enormously
between arms, so anything those requests leave behind in process-level state
reaches this view differently in each arm.

This isolates it. Same view, same reps, nothing measured before it.

    perf/dc.sh exec -T nautobot python /source/perf/probe_ui_only.py
"""

import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

REPS = 5
URL = "/dcim/interface-connections/"


class Counter:
    def __init__(self):
        self.n = 0

    def __call__(self, execute, sql, params, many, context):
        self.n += 1
        return execute(sql, params, many, context)


client = get_perf_client()
print(f"{'limit':>6} {'queries':>8} {'median':>9}")
for limit in (5, 25, 50):
    target = f"{URL}?limit={limit}"
    client.get(target)  # warm
    counter = Counter()
    with connection.execute_wrapper(counter):
        client.get(target)
    timings = []
    for _ in range(REPS):
        start = time.perf_counter()
        client.get(target)
        timings.append((time.perf_counter() - start) * 1000.0)
    timings.sort()
    print(f"{limit:6d} {counter.n:8d} {timings[len(timings) // 2]:8.1f}ms")
