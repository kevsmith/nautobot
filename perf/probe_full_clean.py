#!/usr/bin/env python
"""Which part of full_clean() issues 18 status queries per cable?

The ORM attribution put `SELECT 1 FROM extras_status INNER JOIN
extras_status_content_types ...` at 18 per cable created, attributed to
`validated_save` -- which is just `full_clean()` then `save()`. Django's
full_clean has four phases (clean_fields, clean, validate_unique,
validate_constraints) and the stack frame alone does not say which one, nor why
it repeats.

Count each phase per model, and print the complete stack for the first few of
the query in question.

    perf/dc.sh exec -T nautobot python /source/perf/probe_full_clean.py
"""

import argparse
from collections import Counter
import os
import sys
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.db import connection, models, transaction  # noqa: E402

from nautobot.extras.context_managers import web_request_context  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1w_writes import Rollback  # noqa: E402

PHASES = ("full_clean", "clean_fields", "clean", "validate_unique", "validate_constraints")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="extras_status_content_types")
    ap.add_argument("--stacks", type=int, default=2)
    args = ap.parse_args()

    from nautobot.dcim.constants import CONTENT_TYPE_TO_TERMINATION_FK
    from nautobot.dcim.models import Cable, CableToCableTermination

    user = get_user_model().objects.filter(username="perfbot").first()

    cable = Cable.objects.order_by("pk").first()
    spec = {"status_id": cable.status_id, "type": cable.type, "ends": []}
    for end in ("a", "b"):
        ct_id = getattr(cable, f"termination_{end}_type_id")
        obj_id = getattr(cable, f"termination_{end}_id")
        if ct_id and obj_id:
            spec["ends"].append((end.upper(), ContentType.objects.get(pk=ct_id), obj_id))
    pk = cable.pk

    calls = Counter()
    originals = {name: getattr(models.Model, name) for name in PHASES}

    def make(name, original):
        def wrapped(self, *a, **kw):
            calls[(type(self).__name__, name)] += 1
            return original(self, *a, **kw)

        return wrapped

    seen = {"n": 0}
    matched = Counter()
    totals = Counter()

    def explain(execute, sql, params, many, context):
        totals["all"] += 1
        if args.match in sql:
            frames = [f for f in traceback.extract_stack() if "/perf/" not in f.filename]
            matched[frames[-1].name] += 1
            if seen["n"] < args.stacks:
                seen["n"] += 1
                print(f"--- stack {seen['n']} for {args.match} ---")
                for f in frames[-14:]:
                    where = f.filename.split("/site-packages/")[-1].split("/source/")[-1]
                    print(f"      {where}:{f.lineno} {f.name}")
                print()
        return execute(sql, params, many, context)

    for name in PHASES:
        setattr(models.Model, name, make(name, originals[name]))
    try:
        with connection.execute_wrapper(explain), transaction.atomic():
            Cable.objects.filter(pk=pk).delete()
            calls.clear()
            matched.clear()
            seen["n"] = 0
            with web_request_context(user, context_detail="probe-full-clean"):
                new = Cable(status_id=spec["status_id"], type=spec["type"])
                new.validated_save()
                for cable_end, ct, obj_id in spec["ends"]:
                    fk = CONTENT_TYPE_TO_TERMINATION_FK[(ct.app_label, ct.model)]
                    CableToCableTermination(
                        cable=new, cable_end=cable_end, connector=1, **{f"{fk}_id": obj_id}
                    ).validated_save()
            raise Rollback
    except Rollback:
        pass
    finally:
        for name in PHASES:
            setattr(models.Model, name, originals[name])

    print("validation phases invoked while creating ONE cable:\n")
    print(f"{'model':36s} " + " ".join(f"{p:>13}" for p in PHASES))
    models_seen = sorted({m for m, _ in calls})
    for model in models_seen:
        print(f"{model:36s} " + " ".join(f"{calls[(model, p)]:>13}" for p in PHASES))
    print(f"\ntotal queries for one cable create: {totals['all']}")
    print(f"{args.match!r} queries: {sum(matched.values())}")


if __name__ == "__main__":
    main()
