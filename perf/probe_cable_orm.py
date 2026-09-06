#!/usr/bin/env python
"""How much of a cable create is the REST layer, and how much is the model?

The attribution probe measured 207 queries for a cable created over REST. Some of
that is unavoidable for anyone creating a cable -- `rebuild_paths` runs from a
post_save signal, and change logging serializes the object through its full API
serializer whatever called it. The rest is the serializer: DRF list validation,
`_apply_terminations`, and rendering the created object back to the client.

Knowing which is which decides where a fix would go, so build the same cables
through the ORM and compare. Same terminations, same change context, no HTTP.

    perf/dc.sh exec -T nautobot python /source/perf/probe_cable_orm.py --n 5
"""

import argparse
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.db import connection, transaction  # noqa: E402

from nautobot.extras.context_managers import web_request_context  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_cable_attribution import StackCollector  # noqa: E402
from tier1w_writes import Rollback  # noqa: E402


def snapshot(Cable, n):
    """Existing cables, described well enough to rebuild them through the ORM."""
    out, pks = [], []
    for cable in Cable.objects.order_by("pk")[:n]:
        pks.append(cable.pk)
        ends = []
        for end in ("a", "b"):
            ct_id = getattr(cable, f"termination_{end}_type_id")
            obj_id = getattr(cable, f"termination_{end}_id")
            if ct_id and obj_id:
                ends.append((end.upper(), ContentType.objects.get(pk=ct_id), obj_id))
        out.append({"status_id": cable.status_id, "type": cable.type, "ends": ends})
    return out, pks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    from nautobot.dcim.constants import CONTENT_TYPE_TO_TERMINATION_FK
    from nautobot.dcim.models import Cable, CableToCableTermination

    User = get_user_model()
    user = User.objects.filter(username="perfbot").first()
    specs, pks = snapshot(Cable, args.n)

    collector = StackCollector()
    with connection.execute_wrapper(collector):
        try:
            with transaction.atomic():
                Cable.objects.filter(pk__in=pks).delete()
                deleted_at = collector.total
                # The delete cascades and rebuilds paths, so its queries must not be
                # folded into the per-cable rates. Counting them was a real defect:
                # it inflated the ORM status-validation figure from 4 to 18 per cable.
                collector.sites.clear()
                collector.shapes.clear()
                with web_request_context(user, context_detail="probe-cable-orm"):
                    for spec in specs:
                        cable = Cable(status_id=spec["status_id"], type=spec["type"])
                        cable.validated_save()
                        for cable_end, ct, obj_id in spec["ends"]:
                            fk = CONTENT_TYPE_TO_TERMINATION_FK[(ct.app_label, ct.model)]
                            CableToCableTermination(
                                cable=cable, cable_end=cable_end, connector=1, **{f"{fk}_id": obj_id}
                            ).validated_save()
                raise Rollback
        except Rollback:
            pass

    created = collector.total - deleted_at
    print(
        f"\n=== ORM: {args.n} cables, {collector.total} queries "
        f"({deleted_at} delete, {created / args.n:.0f} per cable created) ===\n"
    )
    for key, count in collector.sites.most_common(args.top):
        shapes = collector.shapes[key]
        print(f"  {count:>5}  ({count / args.n:>5.1f}/cable)  {len(shapes)} distinct SQL shape(s) from:")
        for frame in key:
            print(f"         {frame}")
        for shape, shape_count in shapes.most_common(3):
            print(f"           {shape_count:>5} ({shape_count / args.n:>5.1f}/cable)  {shape[:92]}")
        print()
    print(f"  {len(collector.sites)} distinct call sites")


if __name__ == "__main__":
    main()
