#!/usr/bin/env python
"""Payload control for the Device LLDP neighbors tab.

`probe_endpoint_ab.py`'s body digest is not usable on a UI page: it carries a per-request CSRF
token, so it differs between two requests in the same process on the same arm. This strips the
tokens and prints a digest of what is left, plus the rendered interface -> connected-endpoint
pairs, which is the content the prefetch under test could change.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_lldp_equivalence.py
"""

import hashlib
import os
import re
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db.models import Count  # noqa: E402
from django.urls import reverse  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

from nautobot.dcim.models import Device  # noqa: E402

VOLATILE = [
    re.compile(rb'name="csrfmiddlewaretoken" value="[^"]*"'),
    re.compile(rb'csrfToken:\s*"[^"]*"'),
    re.compile(rb'value="[A-Za-z0-9]{32,}"'),
    # HTMX carries the CSRF token again as a body attribute, which is how this probe's first
    # version still reported two renders of the same page as different.
    re.compile(rb'"x-csrftoken":\s*"[^"]*"'),
    re.compile(rb'[A-Za-z0-9]{60,}'),
]


def stable(body):
    for pattern in VOLATILE:
        body = pattern.sub(b"<volatile>", body)
    return body


def dump(path, body):
    with open(path, "wb") as fh:
        fh.write(body)


def main():
    client = get_perf_client()
    device = Device.objects.annotate(n=Count("interfaces")).order_by("-n").first()
    url = reverse("dcim:device_lldp_neighbors", kwargs={"pk": device.pk})
    bodies = [client.get(url).content for _ in range(2)]
    digests = [hashlib.sha256(stable(b)).hexdigest()[:16] for b in bodies]
    # The rendered rows: every table cell, in order, with tags and whitespace removed.
    text = re.sub(rb"<[^>]+>", b" ", stable(bodies[0]))
    text = b" ".join(text.split())
    print(f"  device {device.name!r} ({device.pk})")
    print(f"  stable digest (2 requests): {digests[0]} {digests[1]} {'SAME' if digests[0] == digests[1] else 'DIFFER'}")
    print(f"  rendered text digest: {hashlib.sha256(text).hexdigest()[:16]}  length {len(text)}")
    out = os.environ.get("PERF_DUMP")
    if out:
        dump(out, stable(bodies[0]))
        print(f"  wrote {out}")
    if digests[0] != digests[1]:
        a, b = stable(bodies[0]), stable(bodies[1])
        shown = 0
        for i in range(min(len(a), len(b))):
            if a[i] != b[i] and shown < 3:
                lo, hi = max(0, i - 70), i + 70
                print(f"      differs at byte {i}:")
                print(f"        A: {a[lo:hi]!r}")
                print(f"        B: {b[lo:hi]!r}")
                shown += 1
                break


if __name__ == "__main__":
    main()
