#!/usr/bin/env python
"""Run EXPLAIN (ANALYZE, BUFFERS) over the queries one endpoint issues.

`dcim-api:device-list` at `?depth=1` spends 441ms of a 658ms request inside the database
across 11 queries -- roughly 40ms each, where the rest of the read surface averages under 3.
Finding 62 attributes that to row width: at `?depth>=1` the nested serializer reads the joined
columns, so the optimizer keeps the JOIN, and Device widens from 32 columns to 212 across 16
foreign keys.

That is an attribution, not a plan. A missing index, a sequential scan, or a bad join order
would produce the same symptom, and none of this branch's instruments can tell those apart from
"the rows are simply wide". This asks PostgreSQL directly.

Reports, per query: the scan nodes it chose, whether any is a sequential scan over a large
table, actual time against estimate, and buffer counts. A plan whose cost is spread evenly over
index scans of wide tables is the row-width story. A sequential scan, or an actual row count far
from the estimate, is a different problem with a different fix.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_query_plan.py \\
        "/api/dcim/devices/?depth=1&limit=100"
"""

import argparse
import json
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

# A sequential scan over a handful of rows is the planner being sensible, not a finding.
SEQ_SCAN_ROWS_OF_INTEREST = 500


def walk(node, depth=0):
    """Yield (depth, node) for a plan tree."""
    yield depth, node
    for child in node.get("Plans", []) or []:
        yield from walk(child, depth + 1)


def summarize(plan_json, sql, index, min_ms):
    root = plan_json[0]["Plan"]
    total = plan_json[0].get("Execution Time", 0.0)
    if total < min_ms:
        return None
    nodes = list(walk(root))
    seq = [n for _, n in nodes if n["Node Type"] == "Seq Scan" and n.get("Actual Rows", 0) >= SEQ_SCAN_ROWS_OF_INTEREST]
    # Estimation error on the node that actually cost the most: a planner working from bad
    # statistics picks bad joins, and that reads as "the database is slow" from outside.
    worst = max(nodes, key=lambda p: p[1].get("Actual Total Time", 0.0))[1]
    est, act = worst.get("Plan Rows", 0), worst.get("Actual Rows", 0)
    ratio = (act / est) if est else float("inf") if act else 1.0
    return {
        "index": index,
        "execution_ms": round(total, 1),
        "planning_ms": round(plan_json[0].get("Planning Time", 0.0), 1),
        "nodes": len(nodes),
        "scan_types": sorted({n["Node Type"] for _, n in nodes if "Scan" in n["Node Type"]}),
        "seq_scans_over_threshold": [f"{n.get('Relation Name')} ({n.get('Actual Rows')} rows)" for n in seq],
        "hottest_node": worst["Node Type"],
        "hottest_relation": worst.get("Relation Name"),
        "hottest_ms": round(worst.get("Actual Total Time", 0.0), 1),
        "rows_estimated_vs_actual": f"{est} vs {act}",
        "estimate_off_by": round(ratio, 1),
        "shared_read_blocks": worst.get("Shared Read Blocks"),
        "shared_hit_blocks": worst.get("Shared Hit Blocks"),
        "sql": " ".join(sql.split())[:160],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--min-ms", type=float, default=1.0, help="only report queries over this")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_perf_client()
    # One warm request first: the first pass pays for process-level caches that have nothing to
    # do with the plan, and would land on whichever query happened to be first.
    client.get(args.url)

    with CaptureQueriesContext(connection) as ctx:
        response = client.get(args.url)
    print(f"{args.url}  status={response.status_code}  queries={len(ctx.captured_queries)}")
    total_django = sum(float(q.get("time", 0) or 0) for q in ctx.captured_queries) * 1000
    print(f"django-reported db time: {total_django:.1f} ms\n")

    out = []
    with connection.cursor() as cursor:
        for i, q in enumerate(ctx.captured_queries):
            sql = q["sql"]
            if not sql.lstrip().upper().startswith("SELECT"):
                continue
            try:
                cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
                plan = cursor.fetchone()[0]
            except Exception as exc:  # a query EXPLAIN cannot take is not a failure of the probe
                print(f"  [{i}] EXPLAIN failed: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            summary = summarize(plan, sql, i, args.min_ms)
            if summary:
                out.append(summary)

    out.sort(key=lambda s: -s["execution_ms"])
    if args.json:
        print(json.dumps(out, indent=2))
        return
    for s in out:
        print(f"[{s['index']}] {s['execution_ms']:.1f} ms exec, {s['planning_ms']:.1f} ms planning, {s['nodes']} nodes")
        print(f"     scans: {', '.join(s['scan_types']) or 'none'}")
        if s["seq_scans_over_threshold"]:
            print(f"     SEQ SCANS: {', '.join(s['seq_scans_over_threshold'])}")
        print(
            f"     hottest: {s['hottest_node']} on {s['hottest_relation']} = {s['hottest_ms']} ms, "
            f"rows est/actual {s['rows_estimated_vs_actual']} (off by {s['estimate_off_by']}x)"
        )
        print(f"     buffers: {s['shared_hit_blocks']} hit, {s['shared_read_blocks']} read")
        print(f"     {s['sql']}\n")
    print(
        f"{len(out)} queries over {args.min_ms} ms; EXPLAIN total "
        f"{sum(s['execution_ms'] for s in out):.1f} ms against django's {total_django:.1f} ms"
    )


if __name__ == "__main__":
    main()
