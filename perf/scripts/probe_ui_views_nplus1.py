#!/usr/bin/env python
"""Query counts for the two UI views in the imported series' UI | Views PR.

Neither page is in `perf/workload.yml`, so neither has ever been measured here: the Device
"LLDP Neighbors" tab reads each interface's connected endpoint, and an object's changelog tab
reads each row's `changed_object` -- a GenericForeignKey, which cannot be JOINed.

Reports the query count and, where the page has a page-size control, the slope per row. The
device chosen is the one with the most interfaces, matching the `worstcase` scenarios in the
workload.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_ui_views_nplus1.py
"""

import collections
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import reverse  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

from nautobot.dcim.models import Device  # noqa: E402
from nautobot.extras.models import ObjectChange  # noqa: E402


import re  # noqa: E402


def normalize(sql):
    """Collapse literals so repetitions of one query shape group together."""
    sql = re.sub(r"'[^']*'", "?", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    sql = re.sub(r"IN \([^)]*\)", "IN (?)", sql)
    return " ".join(sql.split())[:110]


def measure(client, url, **extra):
    client.get(url, **extra)  # warm-up, discarded
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(url, **extra)
    shapes = collections.Counter(normalize(q["sql"]) for q in ctx.captured_queries)
    return resp.status_code, len(ctx.captured_queries), shapes


def main():
    client = get_perf_client()
    device = Device.objects.annotate(n=Count("interfaces")).order_by("-n").first()
    interfaces = device.interfaces.count()

    # The changelog tab is only interesting on an object that has one: pick the Device with the most
    # ObjectChange rows, which is not necessarily the device with the most interfaces.
    busiest = (
        ObjectChange.objects.filter(changed_object_type__app_label="dcim", changed_object_type__model="device")
        .values("changed_object_id")
        .annotate(n=Count("id"))
        .order_by("-n")
        .first()
    )
    logged = Device.objects.filter(pk=busiest["changed_object_id"]).first() if busiest else device
    logged_changes = busiest["n"] if busiest else 0

    rows = [
        ("device.lldp_neighbors", reverse("dcim:device_lldp_neighbors", kwargs={"pk": device.pk}), {}, interfaces),
        ("device.changelog", reverse("dcim:device_changelog", kwargs={"pk": logged.pk}), {}, None),
        (
            "device.changelog.rows",
            reverse("dcim:device_changelog", kwargs={"pk": logged.pk}),
            {"HTTP_HX_REQUEST": "true"},
            None,
        ),
        (
            "device.changelog.rows.100",
            reverse("dcim:device_changelog", kwargs={"pk": logged.pk}) + "?per_page=100",
            {"HTTP_HX_REQUEST": "true"},
            None,
        ),
        ("objectchange.list", reverse("extras:objectchange_list"), {}, None),
        (
            "objectchange.list.rows.100",
            reverse("extras:objectchange_list") + "?per_page=100",
            {"HTTP_HX_REQUEST": "true"},
            None,
        ),
        ("objectchange.list.rows", reverse("extras:objectchange_list"), {"HTTP_HX_REQUEST": "true"}, None),
    ]
    # A count that falls could be many things; a count that does not grow with the number of
    # interfaces is a grouped prefetch. Three devices of different sizes make that visible.
    sized, seen_sizes = [], set()
    for candidate in Device.objects.annotate(n=Count("interfaces")).filter(n__gt=0).order_by("-n"):
        if candidate.n not in seen_sizes:
            seen_sizes.add(candidate.n)
            sized.append(candidate)
        if len(sized) == 3:
            break
    for candidate in sized:
        url = reverse("dcim:device_lldp_neighbors", kwargs={"pk": candidate.pk})
        _, queries, _ = measure(client, url)
        n = candidate.interfaces.count()
        print(f"  lldp scaling: {candidate.name:24s} {n:>3} interfaces -> {queries:>4} queries ({queries / n:.3f}/iface)")

    print(
        f"  interfaces device {device.name!r}: {interfaces} interfaces | "
        f"changelog device {logged.name!r}: {logged_changes} changes | "
        f"ObjectChange rows total: {ObjectChange.objects.count()}"
    )
    for name, url, extra, scale in rows:
        status, queries, shapes = measure(client, url, **extra)
        note = f"  ({queries / scale:.3f} q per interface)" if scale else ""
        print(f"  {name:26s} status={status} queries={queries:>4}{note}   {url}")
        if os.environ.get("PERF_SHAPES") and queries > 40:
            for sql, n in shapes.most_common(5):
                print(f"      x{n:<4} {sql}")


if __name__ == "__main__":
    main()
