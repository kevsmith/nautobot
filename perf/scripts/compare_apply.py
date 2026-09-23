#!/usr/bin/env python3
"""Turn the whole-workflow apply's result lines into the same shape the screens emit.

The apply row in `perf/baselines/cumulative.json` was hand-entered for as long as it
existed, because `apply_arm.sh` printed its result and nothing collected it. That made it
the one row `verify_report.py` could not recompute -- it says so on every run rather than
letting it pass -- and a hand-typed figure is exactly the kind that drifts from the run it
claims to describe.

Input is the `label=... wall_s=... queries=...` lines `apply_arm.sh` already prints, so
nothing about how the measurement is taken has to change. Arms are grouped by the label
prefix before the last `-r<N>`, and each metric is the median across that arm's rounds.

    perf/scripts/compare_apply.py --baseline-arm stock --current-arm branch \
        --out perf/baselines/apply-ab-next.json perf/results/apply-results.txt

Output matches `compare_screen.py`'s contract closely enough for `build_report.py`'s
`agg["file"]` branch and `verify_report.py`'s recompute to read it: `totals` with
baseline/current/delta/pct per metric, and `coverage`.
"""

import argparse
import json
import pathlib
import re
import statistics
import sys

LINE = re.compile(r"\blabel=(?P<label>\S+).*?\brc=(?P<rc>\d+)\s+wall_s=(?P<wall_s>\d+)\s+"
                  r"queries=(?P<queries>\d+)\s+db_ms=(?P<db_ms>\d+)\s+rows=(?P<rows>\S+)\s+"
                  r"nautobot_hash=(?P<hash>\S+)")


def parse(paths):
    rows = []
    for p in paths:
        for line in pathlib.Path(p).read_text().splitlines():
            m = LINE.search(line)
            if m:
                d = m.groupdict()
                d["arm"] = re.sub(r"-r\d+$", "", d["label"])
                rows.append(d)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", help="files holding apply_arm.sh result lines")
    ap.add_argument("--baseline-arm", default="stock")
    ap.add_argument("--current-arm", default="branch")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = parse(args.results)
    if not rows:
        print("no apply result lines found", file=sys.stderr)
        return 2

    arms = {}
    for r in rows:
        arms.setdefault(r["arm"], []).append(r)
    for want in (args.baseline_arm, args.current_arm):
        if want not in arms:
            print(f"no rounds for arm {want!r}; found {sorted(arms)}", file=sys.stderr)
            return 2

    # A non-zero rc means the apply did not complete, so its wall clock describes a partial
    # run. Refuse rather than quietly median it in with the good rounds.
    bad = [r["label"] for r in rows if r["rc"] != "0"]
    if bad:
        print(f"arms with rc != 0, refusing to compare: {bad}", file=sys.stderr)
        return 2

    # Identical row counts across every arm are what say the arms did the same work. An
    # apply that created fewer objects is faster for a reason that is not the change.
    rowcounts = {r["rows"] for r in rows}
    if len(rowcounts) != 1:
        print(f"arms ended with different row counts, refusing to compare: {sorted(rowcounts)}",
              file=sys.stderr)
        return 2

    # Two arms reporting one hash is the failure mode this branch has actually hit: a
    # toggle that silently no-opped, measured six times, caught only by a counter.
    hashes = {a: {r["hash"] for r in rs} for a, rs in arms.items()}
    if hashes[args.baseline_arm] & hashes[args.current_arm]:
        print("both arms report the same content hash -- the swap did not take", file=sys.stderr)
        return 3

    def med(arm, field, scale=1):
        return statistics.median(int(r[field]) for r in arms[arm]) * scale

    totals = {}
    for metric, field, scale in (("queries", "queries", 1), ("db_ms", "db_ms", 1),
                                 ("wall_ms", "wall_s", 1000)):
        b, c = med(args.baseline_arm, field, scale), med(args.current_arm, field, scale)
        totals[metric] = {"baseline": b, "current": c, "delta": c - b,
                          "pct": round((c - b) / b * 100, 2) if b else None}

    n_rounds = min(len(arms[args.baseline_arm]), len(arms[args.current_arm]))
    out = {
        "schema": 1,
        "screen": "apply",
        "coverage": {"comparable": n_rounds, "baseline_records": n_rounds,
                     "rows_per_arm": rowcounts.pop()},
        "arms": {a: {"rounds": len(rs), "hashes": sorted(hashes[a])} for a, rs in arms.items()},
        "totals": totals,
        "measurements": [
            {"id": r["label"], "kind": r["arm"], "wall_ms": int(r["wall_s"]) * 1000,
             "queries": int(r["queries"]), "db_ms": int(r["db_ms"])}
            for r in sorted(rows, key=lambda r: r["label"])
        ],
    }
    pathlib.Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    for m, t in totals.items():
        print(f"  {m:8} {t['baseline']:>12,.0f} -> {t['current']:>12,.0f}  ({t['pct']:+.2f}%)")
    print(f"wrote {args.out} ({n_rounds} rounds per arm)")
    return 0


sys.exit(main())
