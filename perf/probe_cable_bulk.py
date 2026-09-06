#!/usr/bin/env python
"""Why does an array POST of 100 cables return HTTP 500?

Observed during every databot apply of the datacenter dataset: `POST
/api/dcim/cables/` with a batch of 100 returns 500 after 7-10 seconds, 15-17
times per run, and databot falls back to one POST per row. A 10-cable array POST
succeeds (screen_writes.py measures it at 458 queries). Something breaks between
10 and 100.

Two hypotheses, and this probe separates them:

* **Transaction size.** `ModelViewSet.perform_create` wraps the whole array in
  one `transaction.atomic()`. If the failure is the transaction, it reproduces
  in-process and it has a size threshold.
* **The uwsgi layer.** The same run logs `invalid request block size: 8530
  (max 4096)`, so the rig's `buffer-size` is being exceeded by *something*. If
  the failure is there, it cannot reproduce in-process at all.

This drives the Django test client, which never touches uwsgi, so a failure here
is Nautobot's and a clean pass here points at the server in front of it. Every
POST is rolled back, so it leaves no cables behind.

    perf/dc.sh exec -T nautobot python /source/perf/probe_cable_bulk.py
"""

import argparse
import json
import os
import sys
import time
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection, transaction  # noqa: E402
from django.test.client import RequestFactory  # noqa: E402
from django.urls import reverse  # noqa: E402
from rest_framework.request import Request  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from payloads import PayloadBuilder  # noqa: E402
from tier1_queries import get_perf_client  # noqa: E402
from tier1w_writes import QueryCollector, Rollback  # noqa: E402


def payloads_from_existing(Cable, n, termination_type=None):
    """Recreate the create-payload for N existing cables, terminations included.

    ``termination_type`` matters more than it looks. The first cables by pk in
    this dataset are power-port to power-outlet, which terminate immediately.
    databot's are front-port to front-port -- patch panel links, where the cable
    path walks front port to rear port to the next cable and onward. Those are
    different code paths, and only one of them is what the apply actually sends.
    """
    from django.contrib.contenttypes.models import ContentType

    queryset = Cable.objects.order_by("pk")
    if termination_type:
        app_label, model = termination_type.split(".")
        ct = ContentType.objects.get(app_label=app_label, model=model)
        queryset = queryset.filter(termination_a_type=ct)

    out, pks = [], []
    for cable in queryset[:n]:
        pks.append(cable.pk)
        payload = {
            "status": str(cable.status_id),
            "type": cable.type or None,
            "label": cable.label or "",
        }
        for end in ("a", "b"):
            ct_id = getattr(cable, f"termination_{end}_type_id")
            obj_id = getattr(cable, f"termination_{end}_id")
            if ct_id and obj_id:
                ct = ContentType.objects.get(pk=ct_id)
                payload[f"termination_{end}_type"] = f"{ct.app_label}.{ct.model}"
                payload[f"termination_{end}_id"] = str(obj_id)
        out.append({k: v for k, v in payload.items() if v is not None})
    return out, pks


def run_one(client, url, payloads, Cable, delete_pks=None):
    """POST the payload in a rolled-back transaction and report what came back."""
    collector = QueryCollector()
    status = None
    detail = ""
    with connection.execute_wrapper(collector):
        start = time.perf_counter()
        try:
            with transaction.atomic():
                if delete_pks:
                    # The terminations are occupied by the very cables being
                    # recreated, so they have to go first or every payload fails
                    # its occupied-termination check.
                    Cable.objects.filter(pk__in=delete_pks).delete()
                response = client.generic("POST", url, json.dumps(payloads), content_type="application/json")
                status = response.status_code
                if status >= 300:
                    detail = response.content[:2000].decode(errors="replace")
                raise Rollback
        except Rollback:
            pass
        except Exception:
            detail = traceback.format_exc()
        elapsed = (time.perf_counter() - start) * 1000.0

    print(f"n={len(payloads)} status={status} queries={len(collector.sql)} wall_ms={elapsed:.0f}")
    if detail:
        print(detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1,10,25,50,75,100,150")
    ap.add_argument("--model", default="dcim-api:cable-list")
    ap.add_argument("--dump", help="write an N-cable payload here and exit, for POSTing over HTTP")
    ap.add_argument("--dump-n", type=int, default=100)
    ap.add_argument(
        "--from-existing",
        type=int,
        default=0,
        help="rebuild the payload from N cables already in the database, terminations and all",
    )
    ap.add_argument(
        "--free-terminations",
        action="store_true",
        help="with --dump, really delete the source cables so the payload can be POSTed over HTTP",
    )
    ap.add_argument("--termination-type", help="only cables whose A end is this type, e.g. dcim.frontport")
    args = ap.parse_args()

    from nautobot.dcim.api.serializers import CableSerializer
    from nautobot.dcim.models import Cable

    client = get_perf_client()
    context = {"request": Request(RequestFactory().post("/api/")), "depth": 0}
    builder = PayloadBuilder()
    url = reverse(args.model)

    if args.from_existing:
        # The generated payload leaves both terminations null, because they are
        # optional fields and the builder only fills required ones -- so it
        # creates a cable that is connected to nothing and skips every
        # termination check and cable-path walk a real cable pays for. That is
        # not what databot sends. This rebuilds the payload from cables that
        # already exist, so the terminations are real, occupied-checked pairs.
        payloads, pks = payloads_from_existing(Cable, args.from_existing, args.termination_type)
        print(f"rebuilt {len(payloads)} payloads from existing cables")
        if args.dump:
            with open(args.dump, "w") as fh:
                json.dump(payloads, fh)
            print(f"wrote {args.dump}: {os.path.getsize(args.dump)} bytes")
            if args.free_terminations:
                # Committed, not rolled back: an HTTP client cannot share this
                # process's transaction, so the terminations have to be genuinely
                # free before the payload can be POSTed from outside.
                deleted, _ = Cable.objects.filter(pk__in=pks).delete()
                print(f"DELETED {deleted} rows to free the terminations -- run perf/reset_db.sh when finished")
            return
        run_one(client, url, payloads, Cable, delete_pks=pks)
        return

    if args.dump:
        # The same payload the in-process arm accepts, written out so it can be
        # sent through uwsgi. In-process 201 plus HTTP 500 on identical bytes
        # would put the fault in the server in front of Django rather than in
        # Nautobot; identical results on both would put it in databot's payload.
        payloads, _ = builder.build_create(CableSerializer, Cable, context, count=args.dump_n, tag="cable")
        with open(args.dump, "w") as fh:
            json.dump(payloads, fh)
        print(f"wrote {args.dump}: {args.dump_n} cables, {os.path.getsize(args.dump)} bytes")
        return

    print(f"{'n':>5} {'status':>7} {'queries':>9} {'wall_ms':>9}  detail")
    for n in [int(x) for x in args.sizes.split(",")]:
        try:
            payloads, _ = builder.build_create(CableSerializer, Cable, context, count=n, tag="cable")
        except Exception as exc:
            print(f"{n:5d} {'-':>7} {'-':>9} {'-':>9}  could not build payloads: {exc}")
            continue

        body = payloads[0] if n == 1 else payloads
        collector = QueryCollector()
        status = None
        detail = ""
        with connection.execute_wrapper(collector):
            start = time.perf_counter()
            try:
                with transaction.atomic():
                    response = client.generic("POST", url, json.dumps(body), content_type="application/json")
                    status = response.status_code
                    if status >= 300:
                        detail = response.content[:300].decode(errors="replace")
                    raise Rollback
            except Rollback:
                pass
            except Exception:
                # The test client re-raises whatever the view raised, which is the
                # whole point: this is the traceback the 500 is hiding.
                detail = traceback.format_exc()
            elapsed = (time.perf_counter() - start) * 1000.0

        print(
            f"{n:5d} {status!s:>7} {len(collector.sql):9d} {elapsed:9.0f}  {detail.splitlines()[-1] if detail else 'ok'}"
        )
        if detail and status is None:
            print(detail)


if __name__ == "__main__":
    main()
