#!/usr/bin/env python3
"""Collapse several rounds of the same arm into one file of per-measurement medians.

`compare_screen.py` takes one file per arm, but wall clock on this branch needs three
alternating rounds -- so an aggregate built from round 1 alone throws away the two rounds
that make it trustworthy. Two figures on this branch dissolved on a third round, which is
precisely why the rounds are run.

Handles both shapes the harness emits:

    screen output   {"measurements": [...]}   identified by (id, kind)
    tier1 / tier2   {"endpoints":    [...]}   identified by id

Every numeric field present on all rounds is replaced by its median; non-numeric fields are
taken from the first round. A measurement missing from any round is dropped and named, so a
collapsed file never quietly describes a smaller set than its inputs.

    perf/scripts/median_rounds.py --out perf/baselines/tier2-57-stock.json \
        perf/results/rl-tier2-stock-r1.json perf/results/rl-tier2-stock-r2.json ...
"""

import argparse
import collections
import json
import statistics
import sys

SHAPES = (("measurements", ("id", "kind")), ("endpoints", ("id",)))


def key_of(row, fields):
    return tuple(row.get(f) for f in fields)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("rounds", nargs="+")
    args = ap.parse_args()
    if len(args.rounds) < 2:
        sys.exit("give at least two rounds; one round needs no collapsing")

    docs = [json.load(open(p)) for p in args.rounds]
    listkey = next((k for k, _ in SHAPES if k in docs[0]), None)
    if listkey is None:
        sys.exit(f"unrecognised shape: no {' or '.join(k for k, _ in SHAPES)} in {args.rounds[0]}")
    idfields = dict(SHAPES)[listkey]

    per = collections.defaultdict(list)
    for doc in docs:
        for row in doc[listkey]:
            per[key_of(row, idfields)].append(row)

    dropped = [(k, len(rows)) for k, rows in per.items() if len(rows) != len(docs)]

    out_rows = []
    for k, rows in per.items():
        if len(rows) != len(docs):
            continue
        merged = dict(rows[0])
        for field in rows[0]:
            values = [r.get(field) for r in rows]
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
                med = statistics.median(values)
                merged[field] = int(round(med)) if all(isinstance(v, int) for v in values) else round(med, 2)
        out_rows.append(merged)

    out = dict(docs[0])
    out[listkey] = out_rows
    out["rounds_collapsed"] = len(docs)
    out["rounds_sources"] = args.rounds
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2)

    print(f"wrote {args.out}: {len(out_rows)} of {len(per)} measurements, median of {len(docs)} rounds")
    if dropped:
        print(f"  dropped {len(dropped)} not present in every round:")
        for k, n in dropped[:10]:
            print(f"    {k} (in {n}/{len(docs)})")


if __name__ == "__main__":
    main()
