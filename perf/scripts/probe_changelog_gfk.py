#!/usr/bin/env python
"""Does an object's changelog tab read `changed_object` once per row? Synthesised history.

`UI | Views | 2` in the imported series prefetches the `changed_object` GenericForeignKey on the
two changelog views that build their table directly -- the tab in `core/views/renderers.py` and
`extras.views.ObjectChangeLogView`. This tree already declares
`add_conditional_prefetch("object_repr", "changed_object")` on `ObjectChangeTable`, which applies
whenever the `object_repr` column is visible and the table's data is a queryset, however the table
was constructed. So the question is whether the declaration reaches those two views.

The dataset cannot answer it: 36,552 ObjectChange rows exist but the busiest single object has
**two**, so its tab renders two rows and no N+1 can show. This synthesises history for one device
inside a transaction that is always rolled back, and reports the query count against the number of
rows rendered. Flat means covered; growing means the imported change is still needed here.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_changelog_gfk.py
"""

import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import reverse  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

from nautobot.dcim.models import Device  # noqa: E402
from nautobot.extras.choices import ObjectChangeActionChoices  # noqa: E402
from nautobot.extras.models import ObjectChange  # noqa: E402

ROWS = int(os.environ.get("PERF_ROWS", "100"))


class Rollback(Exception):
    """Raised to unwind the transaction once the measurement is taken."""


def build(devices, per_device):
    """Create `per_device` ObjectChange rows for each device, pointing at that device."""
    ct = ContentType.objects.get_for_model(Device)
    user = get_perf_client  # placeholder to keep the import honest; user is set below
    del user
    changes = []
    for device in devices:
        for i in range(per_device):
            changes.append(
                ObjectChange(
                    user_name="perfbot",
                    request_id=f"00000000-0000-0000-0000-{i:012d}",
                    action=ObjectChangeActionChoices.ACTION_UPDATE,
                    changed_object_type=ct,
                    changed_object_id=device.pk,
                    related_object_type=None,
                    related_object_id=None,
                    object_repr=str(device),
                    object_data={"name": device.name},
                    object_data_v2={"name": device.name},
                )
            )
    ObjectChange.objects.bulk_create(changes)


def measure(client, url, **extra):
    client.get(url, **extra)  # warm-up, discarded
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(url, **extra)
    # Count the synthesised rows actually rendered: a flat query count over rows that never made it
    # onto the page would prove nothing at all.
    rendered = resp.content.count(b"perfbot")
    return resp.status_code, len(ctx.captured_queries), rendered


def main():
    client = get_perf_client()
    device = Device.objects.first()
    results = []
    try:
        with transaction.atomic():
            build([device], ROWS)
            tab = reverse("dcim:device_changelog", kwargs={"pk": device.pk})
            for label, url, extra in (
                ("device.changelog.tab 25", f"{tab}?per_page=25", {"HTTP_HX_REQUEST": "true"}),
                ("device.changelog.tab 100", f"{tab}?per_page=100", {"HTTP_HX_REQUEST": "true"}),
                ("extras.changelog 25", f"{reverse('extras:objectchange_list')}?per_page=25", {"HTTP_HX_REQUEST": "true"}),
                ("extras.changelog 100", f"{reverse('extras:objectchange_list')}?per_page=100", {"HTTP_HX_REQUEST": "true"}),
            ):
                results.append((label, *measure(client, url, **extra)))
            raise Rollback
    except Rollback:
        pass

    assert not ObjectChange.objects.filter(user_name="perfbot").exists(), "rollback failed"
    for label, status, queries, rendered in results:
        print(f"  {label:28s} status={status} queries={queries:>4}  synthesised rows rendered={rendered:>4}")
    print(f"  (rolled back; synthesised rows remaining: {ObjectChange.objects.filter(user_name='perfbot').count()})")


if __name__ == "__main__":
    main()
