#!/usr/bin/env python3
"""Diff a Tier 1 run against a committed baseline and gate on regressions.

This is what makes the optimize loop self-checking: an experiment is kept only
if this exits 0 and shows a real improvement.

The hard invariant is availability -- a working endpoint must not start
erroring. Response content and row ordering are allowed to change; they are
reported for information, not gated on.

    python3 perf/compare.py --baseline perf/baselines/tier1-baseline.json \
        --current perf/results/tier1-current.json
"""

import argparse
import json
import sys

QUERY_TOLERANCE = 0
OK_STATUSES = (200, 302)


def load(path):
    with open(path) as fh:
        data = json.load(fh)
    return {(r["id"], r["url"]): r for r in data["endpoints"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--current", required=True)
    ap.add_argument("--json-out")
    args = ap.parse_args()

    base, curr = load(args.baseline), load(args.current)

    broke_5xx = []        # hard fail: was working, now erroring
    broke_4xx = []        # hard fail: was working, now unreachable
    status_notes = []     # other status movement, informational
    content_changed = []  # informational: output differs
    regressions = []
    improvements = []
    baseline_broken = []

    for key, c in sorted(curr.items()):
        b = base.get(key)
        if b is None:
            continue
        view, url = key
        bs, cs = b["status"], c["status"]

        if bs not in OK_STATUSES:
            baseline_broken.append({"id": view, "url": url,
                                    "detail": f"baseline status {bs}"
                                              f"{' / ' + str(b['error']) if b.get('error') else ''}"})

        if bs in OK_STATUSES and cs != bs:
            entry = {"id": view, "url": url, "detail": f"status {bs} -> {cs}"}
            if cs is None or cs >= 500:
                # An unhandled exception surfaces as status None from the client.
                entry["detail"] += f" {c.get('error') or ''}".rstrip()
                broke_5xx.append(entry)
            elif cs >= 400:
                broke_4xx.append(entry)
            else:
                status_notes.append(entry)
            continue

        if b.get("content_hash") != c.get("content_hash"):
            kind = "reordered" if (b.get("order_hash") and c.get("order_hash")
                                   and b["order_hash"] != c["order_hash"]) else "content differs"
            content_changed.append({"id": view, "url": url, "detail": kind,
                                    "bytes": [b["response_bytes"], c["response_bytes"]]})

        delta = c["query_count"] - b["query_count"]
        entry = {"id": view, "url": url,
                 "queries": [b["query_count"], c["query_count"]], "delta": delta,
                 "duplicates": [b["duplicate_queries"], c["duplicate_queries"]]}
        if delta > QUERY_TOLERANCE:
            regressions.append(entry)
        elif delta < -QUERY_TOLERANCE:
            improvements.append(entry)

    improvements.sort(key=lambda e: e["delta"])
    regressions.sort(key=lambda e: -e["delta"])

    def show(title, rows):
        if not rows:
            return
        print(f"\n{title} ({len(rows)})")
        for r in rows[:40]:
            print(f"  {r['delta']:+5d} queries  {r['queries'][0]:>5} -> {r['queries'][1]:<5}  {r['id']}")
        if len(rows) > 40:
            print(f"  ... and {len(rows) - 40} more")

    if baseline_broken:
        print(f"\nBROKEN AT BASELINE -- pre-existing, not gated ({len(baseline_broken)})")
        for r in baseline_broken[:15]:
            print(f"  {r['id']}: {r['detail']}")
        if len(baseline_broken) > 15:
            print(f"  ... and {len(baseline_broken) - 15} more")

    if content_changed:
        print(f"\nOUTPUT CHANGED -- allowed, informational ({len(content_changed)})")
        for r in content_changed[:15]:
            print(f"  {r['id']}: {r['detail']} {r['bytes'][0]} -> {r['bytes'][1]} bytes")
        if len(content_changed) > 15:
            print(f"  ... and {len(content_changed) - 15} more")

    if status_notes:
        print(f"\nSTATUS MOVED (non-error) ({len(status_notes)})")
        for r in status_notes[:15]:
            print(f"  {r['id']}: {r['detail']}")

    for title, rows in (("ENDPOINTS NOW ERRORING (5xx)", broke_5xx),
                        ("ENDPOINTS NOW UNREACHABLE (4xx)", broke_4xx)):
        if rows:
            print(f"\n{title} ({len(rows)})")
            for r in rows:
                print(f"  {r['id']}: {r['detail']}")

    show("IMPROVEMENTS", improvements)
    show("REGRESSIONS", regressions)

    shared = [k for k in curr if k in base]
    base_total = sum(base[k]["query_count"] for k in shared)
    curr_total = sum(curr[k]["query_count"] for k in shared)
    pct = (curr_total - base_total) / base_total * 100.0 if base_total else 0.0
    print(f"\nTOTAL queries {base_total} -> {curr_total} ({pct:+.1f}%)")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"broke_5xx": broke_5xx, "broke_4xx": broke_4xx,
                       "status_notes": status_notes, "content_changed": content_changed,
                       "baseline_broken": baseline_broken, "regressions": regressions,
                       "improvements": improvements,
                       "total_queries": [base_total, curr_total]}, fh, indent=2)

    if broke_5xx or broke_4xx:
        print(f"\nFAIL: {len(broke_5xx) + len(broke_4xx)} endpoint(s) broken by this change")
        return 2
    if regressions:
        print("\nFAIL: query regressions")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
