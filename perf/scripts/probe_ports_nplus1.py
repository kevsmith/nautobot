#!/usr/bin/env python
"""Do the FrontPort and RearPort API viewsets carry a per-row cable query? Synthesised rows.

`perf/dataset-gaps.md` records that `frontport` and `rearport` return **zero rows** on both
snapshots, so neither the read screen nor any endpoint probe can show an N+1 there: a table
with no rows cannot iterate. Seven of the nine cable-termination viewsets prefetch
`cable_peer_prefetch_related_fields()`; these two do not, and since neither is a `PathEndpoint`
their `connection_prefetch_related_fields()` returns an empty list, so today they prefetch
nothing at all.

This builds the rows the dataset lacks -- inside a transaction that is always rolled back --
and measures the query slope per row. Synthesised rows measure the *shape* of the defect, not
its cost on any real instance; a finding based on this must say so.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_ports_nplus1.py
"""

import json
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection, transaction  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

from nautobot.dcim.choices import PortTypeChoices  # noqa: E402
from nautobot.dcim.models import Device, FrontPort, RearPort  # noqa: E402

ROWS = int(os.environ.get("PERF_ROWS", "100"))
PAGE_SIZES = [25, ROWS]


class Rollback(Exception):
    """Raised to unwind the transaction once the measurement is taken."""


def build(device, count):
    """Create `count` RearPort/FrontPort pairs on `device`, returning nothing."""
    rears = RearPort.objects.bulk_create(
        [
            RearPort(device=device, name=f"perf-rear-{i}", type=PortTypeChoices.TYPE_8P8C, positions=1)
            for i in range(count)
        ]
    )
    FrontPort.objects.bulk_create(
        [
            FrontPort(
                device=device,
                name=f"perf-front-{i}",
                type=PortTypeChoices.TYPE_8P8C,
                rear_port=rear,
                rear_port_position=1,
            )
            for i, rear in enumerate(rears)
        ]
    )


def measure(client, url, limit):
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}limit={limit}"
    client.get(full)  # warm-up, discarded: process caches are cold once only
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(full)
    body = json.loads(resp.content)
    return resp.status_code, len(body.get("results", [])), len(ctx.captured_queries)


def main():
    client = get_perf_client()
    device = Device.objects.first()
    if device is None:
        sys.exit("no devices in the dataset; nothing to hang ports off")

    results = []
    try:
        with transaction.atomic():
            build(device, ROWS)
            for url in ("/api/dcim/front-ports/", "/api/dcim/front-ports/?depth=1",
                        "/api/dcim/rear-ports/", "/api/dcim/rear-ports/?depth=1"):
                points = [measure(client, url, limit) for limit in PAGE_SIZES]
                rows = [p[1] for p in points]
                queries = [p[2] for p in points]
                delta = rows[-1] - rows[0]
                slope = (queries[-1] - queries[0]) / delta if delta else 0.0
                results.append((url, points, slope))
            raise Rollback
    except Rollback:
        pass

    assert not FrontPort.objects.filter(name__startswith="perf-front-").exists(), "rollback failed"
    for url, points, slope in results:
        pairs = "  ".join(f"{limit}: {r} rows/{q} q" for limit, (_, r, q) in zip(PAGE_SIZES, points))
        print(f"  {url:42s} {pairs}   slope={slope:.3f} q/row")
    print(f"  (rolled back; FrontPort rows remaining: {FrontPort.objects.count()})")


if __name__ == "__main__":
    main()
