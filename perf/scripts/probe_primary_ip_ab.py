#!/usr/bin/env python
"""A/B one arm of the `primary_ip` select_related experiment, on real workload scenarios.

Takes scenario ids from perf/workload.yml rather than URLs, because the scenarios that
matter here differ from their neighbours only by a request header: Nautobot defers row
rendering to an HTMX follow-up (core/views/renderers.py:96 renders `queryset.none()`
unless `HX-Request` is set), so `ui.device.list` and `ui.device.list.rows` share the path
`/dcim/devices/`. A probe that took URLs on the command line could not tell them apart --
which is exactly how attribute.py spent this branch's whole length attributing the chrome
page whenever it was pointed at a `.rows` scenario.

Reports per scenario:

  queries        total, the deterministic counter that proves the two arms differ
  ipam           queries against ipam_ipaddress, the specific N+1 under test
  cfg            get_settings_or_config calls, counted through the module attribute the
                 property actually reads (`Device.primary_ip` calls it once per access)
  wall           median of PERF_REPS timed reps, one untimed warm-up discarded first
  digest         sha256 of the response body, and whether it held across every rep.
                 select_related must not change a single byte of output; a digest that
                 moves between arms is a correctness failure, not an optimisation.

    perf/scripts/dc.sh exec -T -e PERF_REPS=9 nautobot \
        python /source/perf/scripts/probe_primary_ip_ab.py ui.device.list.rows ...
"""

import hashlib
import os
import re
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402
import workload as workload_mod  # noqa: E402

REPS = int(os.environ.get("PERF_REPS", "9"))

DEFAULT_SCENARIOS = [
    # targets
    "ui.device.list.rows",
    "ui.device.list.page100.rows",
    "ui.device.list.deep.rows",
    "ui.device.list.filtered.rows",
    # controls. ui.device.list is the sharpest one available: same view, same
    # queryset construction, but it renders `.none()`, so it isolates "did the two
    # extra LEFT JOINs make the base query more expensive?" from the rows saving.
    "ui.device.list",
    "ui.devicetype.list.rows",
    "ui.rack.list.rows",
    "ui.interface.list.rows",
]


class QueryCounter:
    """DEBUG is False on the perf stack, so connection.queries stays empty."""

    def __init__(self):
        self.total = 0
        self.by_table = {}

    def __call__(self, execute, sql, params, many, context):
        self.total += 1
        m = re.search(r'FROM "(\w+)"', sql or "")
        table = m.group(1) if m else "?"
        self.by_table[table] = self.by_table.get(table, 0) + 1
        return execute(sql, params, many, context)


class ConfigCallCounter:
    """Wrap the module attribute `Device.primary_ip` actually reads.

    Patching `nautobot.core.models.utils.get_settings_or_config` does not work: the
    consumer imported it by name at module import, so its own module global is the
    only reference that matters. Three probes on this branch reported a plausible
    zero by patching the wrong one.
    """

    def __init__(self, module, name="get_settings_or_config"):
        self.module = module
        self.name = name
        self.n = 0
        self._original = None

    def __enter__(self):
        self._original = getattr(self.module, self.name)

        def counting(*args, **kwargs):
            self.n += 1
            return self._original(*args, **kwargs)

        setattr(self.module, self.name, counting)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.name, self._original)
        return False


def main():
    ids = sys.argv[1:] or DEFAULT_SCENARIOS
    resolved, _ = workload_mod.resolve(workload_mod.DEFAULT_WORKLOAD)
    rows = {r["id"]: r for r in resolved}
    missing = [i for i in ids if i not in rows]
    if missing:
        sys.exit(f"unknown scenario(s): {', '.join(missing)}")

    import nautobot.dcim.models.devices as devices_mod

    client = get_perf_client()
    print(f"reps={REPS}  (one untimed warm-up discarded per scenario)\n")
    print(f"{'scenario':32s} {'queries':>7} {'ipam':>5} {'cfg':>5} {'median':>9} {'min':>8}  digest")

    for sid in ids:
        row = rows[sid]
        url, headers = row["url"], row.get("headers") or {}

        client.get(url, headers=headers)  # warm-up, discarded

        counter = QueryCounter()
        with ConfigCallCounter(devices_mod) as cfg, connection.execute_wrapper(counter):
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            sys.exit(f"NON-200 {resp.status_code} for {sid} -- measurement void")

        digests = {hashlib.sha256(resp.content).hexdigest()[:16]}
        timings = []
        for _ in range(REPS):
            start = time.perf_counter()
            r = client.get(url, headers=headers)
            timings.append((time.perf_counter() - start) * 1000.0)
            digests.add(hashlib.sha256(r.content).hexdigest()[:16])

        digest = sorted(digests)[0] + ("" if len(digests) == 1 else f" UNSTABLE({len(digests)})")
        print(
            f"{sid:32s} {counter.total:7d} {counter.by_table.get('ipam_ipaddress', 0):5d} "
            f"{cfg.n:5d} {statistics.median(timings):7.1f}ms {min(timings):6.1f}ms  {digest}"
        )


if __name__ == "__main__":
    main()
