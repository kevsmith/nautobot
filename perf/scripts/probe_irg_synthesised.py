#!/usr/bin/env python
"""The accessor-walk gap, measured on synthesised rows.

`probe_accessor_walk_gaps.py` reports exactly one column across 183 table classes whose prefetch is
dropped by the FK guard: `InterfaceRedundancyGroupAssociationTable.interface__ip_addresses`. The
dataset cannot price it -- the largest redundancy group has **two** interfaces -- so this attaches
many interfaces to one group inside a transaction that is always rolled back, and reports the query
count against the number of associations rendered.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_irg_synthesised.py
"""

import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection, transaction  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import reverse  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

from nautobot.dcim.models import Interface, InterfaceRedundancyGroup, InterfaceRedundancyGroupAssociation  # noqa: E402

ROWS = int(os.environ.get("PERF_ROWS", "50"))


class Rollback(Exception):
    """Raised to unwind the transaction once the measurement is taken."""


def main():
    client = get_perf_client()
    group = InterfaceRedundancyGroup.objects.first()
    if group is None:
        sys.exit("no InterfaceRedundancyGroup in this dataset")

    # Interfaces that carry IP addresses, since `interface__ip_addresses` is the dropped path.
    interfaces = list(
        Interface.objects.filter(ip_addresses__isnull=False)
        .exclude(pk__in=group.interfaces.values_list("pk", flat=True))
        .distinct()[:ROWS]
    )
    results = []
    try:
        with transaction.atomic():
            InterfaceRedundancyGroupAssociation.objects.bulk_create(
                [
                    InterfaceRedundancyGroupAssociation(interface_redundancy_group=group, interface=iface, priority=100 + i)
                    for i, iface in enumerate(interfaces)
                ]
            )
            url = reverse("dcim:interfaceredundancygroup", kwargs={"pk": group.pk})
            client.get(url)  # warm-up, discarded
            with CaptureQueriesContext(connection) as ctx:
                resp = client.get(url)
            results.append((len(interfaces), resp.status_code, len(ctx.captured_queries)))
            raise Rollback
    except Rollback:
        pass

    assert InterfaceRedundancyGroupAssociation.objects.filter(priority__gte=100).count() == 0, "rollback failed"
    for added, status, queries in results:
        print(f"  group {group.name!r}: +{added} synthesised associations  status={status}  queries={queries}")
    print(f"  (rolled back; associations remaining: {InterfaceRedundancyGroupAssociation.objects.count()})")


if __name__ == "__main__":
    main()
