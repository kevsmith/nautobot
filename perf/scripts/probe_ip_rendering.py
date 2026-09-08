#!/usr/bin/env python
"""What does displaying an IP address cost, across every surface that displays one?

Started from finding 50's column attribution, where `primary_ip` was 1.789ms/cell -- 178.9ms
of a 779ms request, 37% of all cell rendering -- with 137.1ms of that in accessor resolution
rather than rendering, because `Device.primary_ip` is a property whose first line calls
`get_settings_or_config("PREFER_IPV4")` at ~224us per access.

There are three distinct shapes and they fail differently:

  primary_ip property      device and VM lists, 9 column declarations. One Constance read per
                           access. The if/elif over already-loaded FKs is ~4us; the config
                           call is ~237us, 98% of it.

  INTERFACE_IPADDRESSES    interface tables, via TemplateColumn. Per IP: two URL reversals
                           (`ip.get_absolute_url`, `ip.parent.namespace.get_absolute_url`) and
                           a two-hop FK walk to `ip.parent.namespace`, which is an N+1 unless
                           both hops are prefetched.

  render_ip_with_nat       detail pages only. Calls `ip.nat_outside_list.exists()` per IP, and
                           `.all()` again if it returns True -- one or two queries per IP by
                           construction.

Reports, per surface: total and ipam-table queries, URL reversals split by whether they are
ipam views, Constance reads, and time inside the IP-bearing template column. Query and call
counts are deterministic, so they are usable on a busy box; the millisecond figures are not.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_ip_rendering.py
"""

import argparse
import collections
import functools
import json
import os
import re
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

import django.urls  # noqa: E402
from django.db import connection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

IPAM_TABLE = re.compile(r'"(ipam_\w+)"')

SURFACES = [
    ("ui.device.list.rows", "/dcim/devices/", {"HTTP_HX_REQUEST": "true"}),
    ("ui.interface.list.rows", "/dcim/interfaces/?per_page=100", {"HTTP_HX_REQUEST": "true"}),
    ("ui.ipaddress.list.rows", "/ipam/ip-addresses/?per_page=100", {"HTTP_HX_REQUEST": "true"}),
    ("ui.vm.list.rows", "/virtualization/virtual-machines/", {"HTTP_HX_REQUEST": "true"}),
]


class IPSpy:
    def __init__(self):
        self.queries = 0
        self.ipam_queries = collections.Counter()
        self.reversals = collections.Counter()
        self.config_reads = 0
        self.nat_calls = 0
        self.nat_ms = 0.0
        self.primary_ip_reads = 0

    # --- query wrapper -----------------------------------------------------
    def __call__(self, execute, sql, params, many, context):
        self.queries += 1
        for tbl in set(IPAM_TABLE.findall(sql)):
            self.ipam_queries[tbl] += 1
        return execute(sql, params, many, context)

    def __enter__(self):
        import nautobot.ipam.utils as ipam_utils
        from nautobot.core.utils import config as config_mod

        self._rev = django.urls.reverse
        self._nat = ipam_utils.render_ip_with_nat
        self._cfg = config_mod.get_settings_or_config
        spy = self

        @functools.wraps(self._rev)
        def reverse(viewname, *a, **kw):
            spy.reversals[str(viewname)] += 1
            return spy._rev(viewname, *a, **kw)

        @functools.wraps(self._nat)
        def nat(ip):
            spy.nat_calls += 1
            t = time.perf_counter()
            try:
                return spy._nat(ip)
            finally:
                spy.nat_ms += (time.perf_counter() - t) * 1000.0

        @functools.wraps(self._cfg)
        def cfg(*a, **kw):
            spy.config_reads += 1
            return spy._cfg(*a, **kw)

        django.urls.reverse = reverse
        ipam_utils.render_ip_with_nat = nat
        config_mod.get_settings_or_config = cfg
        self._ipam_utils = ipam_utils
        self._config_mod = config_mod
        return self

    def __exit__(self, *exc):
        django.urls.reverse = self._rev
        self._ipam_utils.render_ip_with_nat = self._nat
        self._config_mod.get_settings_or_config = self._cfg

    def summary(self):
        ipam_rev = sum(n for v, n in self.reversals.items() if v.startswith("ipam:"))
        return {
            "queries": self.queries,
            "ipam_queries": sum(self.ipam_queries.values()),
            "ipam_tables": dict(self.ipam_queries.most_common(4)),
            "reversals": sum(self.reversals.values()),
            "ipam_reversals": ipam_rev,
            "config_reads": self.config_reads,
            "nat_calls": self.nat_calls,
            "nat_ms": round(self.nat_ms, 1),
        }


def measure(client, url, extra, reps):
    client.get(url, **extra)  # warm-up, discarded
    runs = []
    for _ in range(reps):
        spy = IPSpy()
        with spy:
            with connection.execute_wrapper(spy):
                t = time.perf_counter()
                resp = client.get(url, **extra)
                wall = (time.perf_counter() - t) * 1000.0
        s = spy.summary()
        s.update({"wall_ms": wall, "status": resp.status_code, "bytes": len(resp.content)})
        runs.append(s)
    numeric = [k for k, v in runs[0].items() if isinstance(v, (int, float))]
    out = {k: statistics.median([r[k] for r in runs]) for k in numeric}
    out["ipam_tables"] = runs[-1]["ipam_tables"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--only", help="substring filter on the surface id")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_perf_client()
    out = []
    for name, url, extra in SURFACES:
        if args.only and args.only not in name:
            continue
        m = measure(client, url, extra, args.reps)
        m["id"] = name
        out.append(m)
        if not args.json:
            print(f"  {name:24s} status={int(m['status'])} q={int(m['queries']):>4} "
                  f"ipam_q={int(m['ipam_queries']):>4} rev={int(m['reversals']):>4} "
                  f"ipam_rev={int(m['ipam_reversals']):>4} cfg={int(m['config_reads']):>4} "
                  f"nat={int(m['nat_calls']):>3} ({m['nat_ms']:.1f}ms) "
                  f"wall={m['wall_ms']:>7.1f}ms bytes={int(m['bytes']):>7}")
            if m["ipam_tables"]:
                print(f"      ipam tables: {m['ipam_tables']}")
    if args.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
