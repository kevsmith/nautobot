#!/usr/bin/env python
"""Screening pass over every REST read endpoint, normalized to cost per object.

The inner loop measures 38 hand-picked scenarios. That is roughly 5% of the API
surface, and they were chosen for diagnostic interest -- which is a selection
bias, not a sample. `api.interface.depth1` turned out to be a 1,229-query
endpoint and it was found by guessing that interfaces is the biggest table.
There are ~300 endpoints nobody has looked at.

This is a **screening instrument, not a regression gate**. It does not belong in
the inner loop; run it occasionally and read its output as a ranked list of where
to point the next investigation.

Two design choices carry the whole thing:

* **Enumerate from the URL resolver at run time**, never from a list in a file,
  so the matrix cannot rot as models come and go.
* **Normalize to cost per object.** A 5-row model and an 8,925-row one are not
  comparable per request; per returned object they are. Ranking by queries per
  object is what makes an anomaly visible regardless of table size -- a list view
  costing 1.2 queries per row is doing something per row, whatever its size.

Reads only: list, list?depth=1, detail, detail?depth=1. No payloads, no mutation,
safe to re-run against a live dataset.

    perf/dc.sh exec -T nautobot python /source/perf/screen_reads.py \\
        --out /source/perf/results/screen-reads.json
"""

import argparse
import json
import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.urls import get_resolver, NoReverseMatch, reverse, URLPattern, URLResolver  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client, measure  # noqa: E402


def view_names():
    """Every fully-qualified URL name the resolver knows, namespaces included."""

    def walk(resolver, ns=()):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                child = (*ns, pattern.namespace) if pattern.namespace else ns
                yield from walk(pattern, child)
            elif isinstance(pattern, URLPattern) and pattern.name:
                yield ":".join((*ns, pattern.name))

    return sorted(set(walk(get_resolver())))


def api_list_views():
    """(name, app) for every REST list endpoint.

    A DRF router names its list route `<basename>-list` inside an `<app>-api`
    namespace, so the resolver is authoritative about what exists.
    """
    out = []
    for name in view_names():
        if ":" not in name or not name.endswith("-list"):
            continue
        namespace, _, _ = name.rpartition(":")
        if not namespace.endswith("-api"):
            continue
        out.append((name, namespace[: -len("-api")]))
    return out


def body_of(response):
    """Parsed JSON body, or None if the response was not JSON."""
    try:
        return json.loads(response.content.decode())
    except Exception:
        return None


def screen(client, name, app, limit, include_detail):
    """Measure one model's read endpoints. Returns a list of records."""
    records = []
    try:
        list_url = reverse(name)
    except NoReverseMatch as exc:
        return [{"id": name, "app": app, "kind": "list", "url": None, "skipped": f"did not reverse: {exc}"}]

    detail_name = name[: -len("-list")] + "-detail"
    first_id = None
    total = None
    results = []

    for kind, url in (("list", f"{list_url}?limit={limit}"), ("list.depth1", f"{list_url}?limit={limit}&depth=1")):
        # Fetch once to warm and to read count/results, then measure the warm
        # request. Measuring the cold one instead would time first-request import
        # and cache population rather than the endpoint.
        warm = client.get(url)
        if warm.status_code == 200:
            parsed = body_of(warm)
            if isinstance(parsed, dict):
                total = parsed.get("count", total)
                results = parsed.get("results") or []
                if results and first_id is None:
                    first_id = results[0].get("id")

        rec = measure(client, url)
        rec.update(
            id=name,
            app=app,
            kind=kind,
            url=url,
            total_rows=total,
            objects=len(results) if warm.status_code == 200 else None,
        )
        records.append(rec)

    if include_detail and first_id:
        try:
            detail_url = reverse(detail_name, args=[first_id])
        except NoReverseMatch:
            return records
        for kind, url in (("detail", detail_url), ("detail.depth1", f"{detail_url}?depth=1")):
            client.get(url)  # warm, as above
            rec = measure(client, url)
            rec.update(id=name, app=app, kind=kind, url=url, objects=1, total_rows=total)
            records.append(rec)

    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=25, help="page size; per-object normalization makes this comparable")
    ap.add_argument("--only", help="substring filter on view name, for a quick pass")
    ap.add_argument("--max-endpoints", type=int, default=0)
    ap.add_argument("--no-detail", action="store_true")
    args = ap.parse_args()

    targets = api_list_views()
    if args.only:
        targets = [t for t in targets if args.only in t[0]]
    if args.max_endpoints:
        targets = targets[: args.max_endpoints]

    print(
        f"screening {len(targets)} list endpoints (limit={args.limit}, detail={'no' if args.no_detail else 'yes'})",
        file=sys.stderr,
    )

    client = get_perf_client()
    records = []
    started = time.perf_counter()
    for i, (name, app) in enumerate(targets, 1):
        got = screen(client, name, app, args.limit, not args.no_detail)
        records.extend(got)
        listing = next((r for r in got if r.get("kind") == "list"), {})
        print(
            f"[{i}/{len(targets)}] {name:52s} "
            f"rows={listing.get('total_rows')} "
            f"q={listing.get('query_count')} "
            f"{listing.get('wall_ms')}ms",
            file=sys.stderr,
        )
        # Written every endpoint: a 30-minute run that dies at minute 25 should
        # still leave usable data behind.
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(
                {
                    "schema": 1,
                    "limit": args.limit,
                    "elapsed_s": round(time.perf_counter() - started, 1),
                    "endpoints": records,
                },
                fh,
                indent=2,
                sort_keys=True,
            )

    print(f"wrote {args.out} -- {len(records)} measurements in {time.perf_counter() - started:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
