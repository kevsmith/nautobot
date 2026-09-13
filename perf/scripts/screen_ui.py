#!/usr/bin/env python
"""Screening pass over every UI list view, normalized to cost per rendered row.

`screen_reads.py` filters for namespaces ending in `-api`, so it has never
touched the UI. 166 REST endpoints are ranked and zero UI endpoints are, while
the only UI coverage that has ever existed is the inner loop's hand-picked
scenarios -- and finding 44 showed 20 of those were measuring a page that
rendered no rows. This screen exists to make the read-versus-write ranking in
`perf/queue.md` rest on data that includes the surface users actually look at.

Two requests per endpoint, because a Nautobot list view is two requests:

    document   what the browser asks for first. Builds its table over
               `queryset.none()`, so it renders chrome and no rows.
    rows       the follow-up the browser fires from `hx-trigger="load"`,
               carrying `HX-Request`. This is where the row work happens.

Measuring only the first is finding 44's trap and reports a list view as cheap
however large the table. Measuring only the second omits the chrome the user also
waits for. Reporting both is the only honest option, and the split is itself a
result: it says what fraction of a list view is fixed and what scales with rows.

**The UI paginates on `per_page`, not on `limit`.** REST takes `limit`, and the
two cannot be mixed -- asking a UI list for `?limit=N` gets a 2-row fragment,
which reads as a fast endpoint rather than as a wrong question.

This is a **screening instrument, not a regression gate**, on the same terms as
the other two screens: run it occasionally, read it as a ranked list.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/screen_ui.py \\
        --out /source/perf/results/screen-ui.json
"""

import argparse
import json
import os
import re
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.urls import get_resolver, NoReverseMatch, reverse, URLPattern, URLResolver  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client, measure  # noqa: E402

HX_HEADERS = {"HX-Request": "true"}

# Same threshold and the same reason as screen_reads.py: an endpoint returning a
# handful of rows is exercised, but its per-row figure is mostly fixed cost spread
# over too few rows to mean anything.
FULL_PAGE_ROWS = 10

TBODY = re.compile(r"<tbody[^>]*>(.*?)</tbody>", re.S | re.I)
TR = re.compile(r"<tr[\s>]", re.I)


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


def ui_list_views():
    """(name, app) for every UI list view.

    Nautobot names a UI list route `<model>_list` inside an `<app>` namespace,
    where the REST equivalent is `<basename>-list` inside `<app>-api`. Excluding
    the `-api` namespaces is what separates the two surfaces; the resolver is
    authoritative about what exists in either.
    """
    out = []
    for name in view_names():
        if ":" not in name or not name.endswith("_list"):
            continue
        namespace, _, _ = name.rpartition(":")
        if namespace.endswith("-api"):
            continue
        out.append((name, namespace))
    return out


def row_count(response):
    """Rendered data rows, counted inside `<tbody>` so the header row is excluded.

    Counting every `<tr>` in the fragment overcounts by one per table, which
    matters precisely where it hurts: an endpoint rendering nothing would report
    one row and be normalized against it.
    """
    try:
        html = response.content.decode("utf-8", "replace")
    except Exception:
        return None
    bodies = TBODY.findall(html)
    if not bodies:
        return 0
    return sum(len(TR.findall(b)) for b in bodies)


def median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def repeated(client, url, reps, headers):
    """GET `url` `reps` times and summarize, after one discarded run.

    Same reporting contract as the other two screens, so `compare_screen.py` and
    `median_rounds.py` need no special case. The discarded run is not optional: a
    rolled-back transaction restores the database and not the process, and the
    request after a cold one still pays for whatever the process was doing before
    the endpoint was touched.
    """
    measure(client, url, headers=headers)
    runs = [measure(client, url, headers=headers) for _ in range(reps)]
    counts = {run["query_count"] for run in runs}
    summary = dict(runs[-1])
    summary["wall_ms"] = round(median([run["wall_ms"] for run in runs]), 2)
    summary["db_ms"] = round(median([run["db_ms"] for run in runs]), 2)
    summary["reps"] = reps
    summary["query_count_stable"] = len(counts) == 1
    if len(counts) > 1:
        summary["query_count_range"] = [min(counts), max(counts)]
    return summary


def per_row(record):
    rows = record.get("rows")
    if not rows:
        return None
    return record["query_count"] / rows


def screen_one(client, name, app, per_page, reps):
    """Both requests for one list view, returned as two records."""
    try:
        path = reverse(name)
    except NoReverseMatch:
        return [
            {"id": name, "app": app, "kind": k, "skipped": "does not reverse without arguments"}
            for k in ("document", "rows")
        ]

    url = f"{path}?per_page={per_page}"
    records = []
    for kind, headers in (("document", None), ("rows", HX_HEADERS)):
        # `measure()` returns a profile, not the response, so the body is read
        # with its own GET. This doubles as the warm request the screens rely on
        # before timing anything.
        try:
            response = client.get(url, follow=False, headers=headers or {})
        except Exception as exc:  # a view that raises is a finding, not a crash
            records.append({"id": name, "app": app, "kind": kind, "skipped": f"raised {type(exc).__name__}"})
            continue
        if response.status_code != 200:
            records.append(
                {
                    "id": name,
                    "app": app,
                    "kind": kind,
                    "status": response.status_code,
                    "skipped": f"status {response.status_code}",
                }
            )
            continue
        rows = row_count(response)
        summary = repeated(client, url, reps, headers)
        summary.update(
            {"id": name, "app": app, "kind": kind, "url": url, "rows": rows, "objects": rows, "skipped": None}
        )
        records.append(summary)
    return records


def coverage_summary(all_endpoints, targets, records):
    rows_records = [r for r in records if r.get("kind") == "rows" and not r.get("skipped")]
    return {
        "resolved": len(all_endpoints),
        "attempted": len(targets),
        "measured": len([r for r in records if not r.get("skipped")]),
        "skipped": len([r for r in records if r.get("skipped")]),
        "exercised": len([r for r in rows_records if (r.get("rows") or 0) >= 1]),
        "full_page": len([r for r in rows_records if (r.get("rows") or 0) >= FULL_PAGE_ROWS]),
        "empty": len([r for r in rows_records if (r.get("rows") or 0) == 0]),
    }


def print_rankings(records, top):
    timed = [r for r in records if not r.get("skipped") and r.get("query_count") is not None]
    rows_only = [r for r in timed if r["kind"] == "rows"]

    # Restricted to full pages. A table rendering one row reports its fixed cost
    # as its per-row cost -- 9 queries over 1 row ranks above a genuine N+1 over
    # 25 -- so an unrestricted ranking here is noise sorted by table size.
    populated = [r for r in rows_only if (r.get("rows") or 0) >= FULL_PAGE_ROWS]
    ranked = [(per_row(r), r) for r in populated if per_row(r) is not None]
    ranked.sort(key=lambda pair: -pair[0])
    print(
        f"\n{'top by queries per rendered row (tables with %d+ rows)' % FULL_PAGE_ROWS:46s} "
        f"{'rows':>5} {'q':>6} {'q/row':>7} {'db_ms':>8} {'wall_ms':>8}"
    )
    for value, r in ranked[:top]:
        print(
            f"{r['id']:46s} {r.get('rows') or 0:>5} {r['query_count']:>6} "
            f"{value:>7.3f} {r['db_ms']:>8.1f} {r['wall_ms']:>8.1f}"
        )

    by_wall = sorted(rows_only, key=lambda r: -(r.get("wall_ms") or 0))
    print(f"\n{'top by wall clock, row request':46s} {'rows':>5} {'q':>6} {'db_ms':>8} {'wall_ms':>8}")
    for r in by_wall[:top]:
        print(f"{r['id']:46s} {r.get('rows') or 0:>5} {r['query_count']:>6} {r['db_ms']:>8.1f} {r['wall_ms']:>8.1f}")

    # The split is the point of measuring both requests, so it is reported rather
    # than left for someone to compute from the JSON.
    docs = {r["id"]: r for r in timed if r["kind"] == "document"}
    pairs = [(r, docs[r["id"]]) for r in rows_only if r["id"] in docs]
    if pairs:
        doc_total = sum(d["wall_ms"] for _, d in pairs)
        row_total = sum(r["wall_ms"] for r, _ in pairs)
        total = doc_total + row_total
        print(f"\nDOCUMENT VERSUS ROWS over {len(pairs)} list views answering on both")
        print(f"  document {doc_total:9.1f} ms  ({doc_total / total * 100:4.1f}% of a full page load)")
        print(f"  rows     {row_total:9.1f} ms  ({row_total / total * 100:4.1f}%)")
        print(f"  total    {total:9.1f} ms")
        # The document is close to a fixed cost per page, so this split moves with
        # how many small tables are in the set. Quoting the all-endpoints figure as
        # though it described a list view a user actually waits on overstates the
        # document, because most tables here render fewer than a full page.
        big = [(r, d) for r, d in pairs if (r.get("rows") or 0) >= FULL_PAGE_ROWS]
        if big:
            bd = sum(d["wall_ms"] for _, d in big)
            br = sum(r["wall_ms"] for r, _ in big)
            print(
                f"  over the {len(big)} tables rendering {FULL_PAGE_ROWS}+ rows: "
                f"document {bd / (bd + br) * 100:4.1f}%, rows {br / (bd + br) * 100:4.1f}%"
            )
        q_doc = sum(d["query_count"] for _, d in pairs)
        q_row = sum(r["query_count"] for r, _ in pairs)
        print(f"  queries: document {q_doc}, rows {q_row}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--per-page", type=int, default=25, help="UI page size; the UI paginates on per_page, never on limit"
    )
    ap.add_argument("--reps", type=int, default=3, help="measured reps after one discarded warmup")
    ap.add_argument("--only", help="substring filter on view name, for a quick pass")
    ap.add_argument("--max-endpoints", type=int, default=0)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    if args.reps < 1:
        ap.error("--reps must be at least 1")

    started = time.perf_counter()
    all_endpoints = ui_list_views()
    targets = all_endpoints
    if args.only:
        targets = [t for t in targets if args.only in t[0]]
    if args.max_endpoints:
        targets = targets[: args.max_endpoints]

    print(f"{len(all_endpoints)} UI list views; screening {len(targets)} at per_page={args.per_page}")
    client = get_perf_client()

    records = []
    for i, (name, app) in enumerate(targets, 1):
        got = screen_one(client, name, app, args.per_page, args.reps)
        records.extend(got)
        rows = next((r for r in got if r["kind"] == "rows"), {})
        if rows.get("skipped"):
            print(f"[{i}/{len(targets)}] {name:46s} skipped: {rows['skipped']}")
        else:
            print(
                f"[{i}/{len(targets)}] {name:46s} rows={rows.get('rows')} "
                f"q={rows.get('query_count')} {rows.get('wall_ms')}ms"
            )
        # Written as we go, so a run killed partway still yields what it measured.
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(
                {
                    "schema": 1,
                    "per_page": args.per_page,
                    "reps": args.reps,
                    "elapsed_s": round(time.perf_counter() - started, 1),
                    "coverage": coverage_summary(all_endpoints, targets, records),
                    "endpoints": records,
                },
                fh,
                indent=2,
                sort_keys=True,
            )

    cov = coverage_summary(all_endpoints, targets, records)
    print(
        f"\ncoverage: {cov['attempted']} attempted / {cov['measured']} measured / "
        f"{cov['exercised']} rendered a row / {cov['full_page']} rendered {FULL_PAGE_ROWS}+ / "
        f"{cov['empty']} rendered none / {cov['skipped']} skipped"
    )
    print_rankings(records, args.top)
    print(f"\nwrote {args.out} -- {len(records)} measurements in {round(time.perf_counter() - started, 1)}s")


if __name__ == "__main__":
    main()
