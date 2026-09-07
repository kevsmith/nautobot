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

Three design choices carry the whole thing:

* **Enumerate from the URL resolver at run time**, never from a list in a file,
  so the matrix cannot rot as models come and go.
* **Normalize to cost per object.** A 5-row model and an 8,925-row one are not
  comparable per request; per returned object they are. Ranking by queries per
  object is what makes an anomaly visible regardless of table size -- a list view
  costing 1.2 queries per row is doing something per row, whatever its size.
* **Count coverage, do not infer it.** 70 of the 166 endpoints this resolves
  return zero rows against the current dataset, so they contribute a measurement
  that says nothing while counting as coverage. The `coverage` block reports
  attempted, exercised and measured separately. Quoting the endpoint total alone
  is the failure finding 37 recorded against this instrument.

Wall clock and db time are the median of `--reps` measured requests, taken after
one discarded one. Everything else is deterministic and comes from the last rep.
One measured request was enough while the output was read as a ranked list of
query counts; a stock-versus-branch comparison on wall clock is not possible from
a single request, which is what added the repetition.

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

# An endpoint returning a handful of rows is exercised, but its per-object figure
# is still mostly fixed overhead spread over too few objects. Finding 37's "49
# exercised" is this threshold, not the at-least-one-row count, which is 96 --
# both are reported so neither number can be quoted as the other.
FULL_PAGE_ROWS = 10


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


def median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def repeated(client, url, reps):
    """GET ``url`` ``reps`` times and summarize.

    Same reporting contract as ``screen_writes.py:repeated()``, so the two
    screens emit the same keys for the same meanings. Wall clock and db time are
    medians: reporting the last rep is what let a recurring GC pause land on one
    arm and not the other (finding 32). Query count is reported as a range when
    it moves, because an unstable count is itself the finding -- it means the
    request's work depends on state the measurement did not control.
    """
    # One discarded run before the first measured one, as the write screen does.
    # The caller has already issued a warm GET to read count/results off the
    # body; this one exists because the request after that first one still pays
    # for whatever the process was doing before the endpoint was touched, and a
    # first rep carrying that cost is exactly the effect finding 32 traced.
    measure(client, url)

    runs = [measure(client, url) for _ in range(reps)]
    counts = {run["query_count"] for run in runs}
    summary = dict(runs[-1])
    summary["wall_ms"] = round(median([run["wall_ms"] for run in runs]), 2)
    summary["db_ms"] = round(median([run["db_ms"] for run in runs]), 2)
    summary["reps"] = reps
    summary["query_count_stable"] = len(counts) == 1
    if len(counts) > 1:
        summary["query_count_range"] = [min(counts), max(counts)]
    return summary


def screen(client, name, app, limit, include_detail, reps):
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

        rec = repeated(client, url, reps)
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
            rec = repeated(client, url, reps)
            rec.update(id=name, app=app, kind=kind, url=url, objects=1, total_rows=total)
            records.append(rec)

    return records


def per_object(record):
    """Queries per returned object, or None when the request returned nothing.

    A zero-row endpoint has no per-object cost: dividing by its row count either
    fails or invents a denominator, and both read as a result.
    """
    objects = record.get("objects")
    if not objects or record.get("query_count") is None:
        return None
    return record["query_count"] / objects


def coverage_summary(all_endpoints, targets, records):
    """Attempted / exercised / measured, each one counted rather than inferred.

    An endpoint that returned zero rows was measured and not exercised, and the
    difference is the whole point: it costs almost nothing per request and so
    reads as cheap, when what happened is that nothing was asked of it. Its list
    view is still timed and still in `endpoints`, because "this model is empty"
    is a fact about the dataset the numbers were taken against.
    """
    rows = {r["id"]: (r.get("objects") or 0) for r in records if r.get("kind") == "list"}
    exercised = sorted(name for name, objects in rows.items() if objects)
    empty = sorted(name for name, objects in rows.items() if not objects)
    return {
        "list_endpoints": len(all_endpoints),
        "attempted": len(targets),
        "exercised": len(exercised),
        "exercised_full_page": sum(1 for objects in rows.values() if objects >= FULL_PAGE_ROWS),
        "full_page_rows": FULL_PAGE_ROWS,
        "measured": sum(1 for r in records if not r.get("skipped")),
        "empty": len(empty),
        "not_reversed": sum(1 for r in records if r.get("skipped")),
        "unstable_query_counts": sum(1 for r in records if r.get("query_count_stable") is False),
        # Named, not just counted: seeding these is the only way the exercised
        # figure moves, so the list is the work item.
        "empty_endpoints": empty,
    }


def print_rankings(records, top):
    """Rank twice, on queries per object and on db time.

    Queries per object finds work done per row, which is what a screen over
    tables of wildly different sizes needs. It is structurally blind to a small
    number of expensive queries: finding 37 has `dcim-api:device-list` spending
    438 ms in the database over 8 queries with no duplicates, ranked 100th by
    queries per object. Neither ranking is the right one on its own.
    """
    timed = [r for r in records if not r.get("skipped") and r.get("query_count") is not None]

    ranked = [(per_object(r), r) for r in timed if per_object(r) is not None]
    ranked.sort(key=lambda pair: -pair[0])
    print(f"\n{'top by queries per object':52s} {'kind':13s} {'rows':>5} {'q':>6} {'q/obj':>7} {'db_ms':>8}")
    for value, r in ranked[:top]:
        print(
            f"{r['id']:52s} {r['kind']:13s} {r.get('objects') or 0:>5} "
            f"{r['query_count']:>6} {value:>7.2f} {r['db_ms']:>8.1f}"
        )

    by_db = sorted(timed, key=lambda r: -(r.get("db_ms") or 0))
    print(f"\n{'top by db time':52s} {'kind':13s} {'db_ms':>8} {'q':>6} {'dup':>6} {'wall_ms':>8}")
    for r in by_db[:top]:
        print(
            f"{r['id']:52s} {r['kind']:13s} {r['db_ms']:>8.1f} "
            f"{r['query_count']:>6} {r.get('duplicate_queries') or 0:>6} {r['wall_ms']:>8.1f}"
        )


def write_out(args, all_endpoints, targets, records, started):
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(
            {
                "schema": 1,
                "limit": args.limit,
                "reps": args.reps,
                "elapsed_s": round(time.perf_counter() - started, 1),
                "coverage": coverage_summary(all_endpoints, targets, records),
                "endpoints": records,
            },
            fh,
            indent=2,
            sort_keys=True,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=25, help="page size; per-object normalization makes this comparable")
    ap.add_argument("--reps", type=int, default=3, help="measured reps after one discarded warmup; wall/db are medians")
    ap.add_argument("--only", help="substring filter on view name, for a quick pass")
    ap.add_argument("--max-endpoints", type=int, default=0)
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--top", type=int, default=20, help="rows per ranking in the closing summary")
    args = ap.parse_args()
    if args.reps < 1:
        ap.error("--reps must be at least 1")

    all_endpoints = api_list_views()
    targets = all_endpoints
    if args.only:
        targets = [t for t in targets if args.only in t[0]]
    if args.max_endpoints:
        targets = targets[: args.max_endpoints]

    print(
        f"{len(all_endpoints)} list endpoints; screening {len(targets)} "
        f"(limit={args.limit}, reps={args.reps}, detail={'no' if args.no_detail else 'yes'})",
        file=sys.stderr,
    )

    client = get_perf_client()
    records = []
    started = time.perf_counter()
    for i, (name, app) in enumerate(targets, 1):
        got = screen(client, name, app, args.limit, not args.no_detail, args.reps)
        records.extend(got)
        listing = next((r for r in got if r.get("kind") == "list"), {})
        unstable = "" if listing.get("query_count_stable", True) else f" q-range={listing.get('query_count_range')}"
        print(
            f"[{i}/{len(targets)}] {name:52s} "
            f"rows={listing.get('total_rows')} "
            f"q={listing.get('query_count')} "
            f"{listing.get('wall_ms')}ms{unstable}",
            file=sys.stderr,
        )
        # Written every endpoint: a 30-minute run that dies at minute 25 should
        # still leave usable data behind.
        write_out(args, all_endpoints, targets, records, started)

    write_out(args, all_endpoints, targets, records, started)
    coverage = coverage_summary(all_endpoints, targets, records)
    print_rankings(records, args.top)
    print(
        f"\ncoverage: {coverage['attempted']} attempted / {coverage['exercised']} exercised / "
        f"{coverage['measured']} measured "
        f"({coverage['exercised_full_page']} returned {FULL_PAGE_ROWS}+ rows, "
        f"{coverage['empty']} returned none, {coverage['not_reversed']} did not reverse)",
        file=sys.stderr,
    )
    if coverage["unstable_query_counts"]:
        print(
            f"   {coverage['unstable_query_counts']} measurement(s) had an unstable query count across reps",
            file=sys.stderr,
        )
    print(f"wrote {args.out} -- {len(records)} measurements in {time.perf_counter() - started:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
