#!/usr/bin/env python3
"""Diff two screening runs -- read screen or write screen -- and emit the deltas.

This exists because the same comparison was done by hand three times (findings
39, 41 and 42) and a by-hand aggregate is not reproducible. The sentence
"260 measurements present on both sides, identical coverage -- -12.9% queries,
-23.9% wall, -12.3% db time, zero measurements worse, 153 of 260 improved" is
every number this script prints, and it now comes out of the same code on the
read path and the write path.

Three rules carry it.

**Match on the stable identifier, never on list position.** The screens
enumerate from the URL resolver at run time, so the two arms can legitimately
contain different endpoints in a different order -- a model added, a payload
that built on one arm and not the other. `(id, kind)` is the identifier;
positional zipping would silently pair `dcim.interface` against
`dcim.interfacetemplate`.

**Aggregate over the matched, comparable set only, and report what fell out.**
Coverage that differs between arms invalidates an aggregate without changing
its sign, which is exactly the error that is hardest to see afterwards. A
measurement counts only if it appears on both sides *and* succeeded on both
sides: finding 39's pair holds 265 measurements each, five of which were HTTP
failures on both arms, and it is the remaining 260 that produce -12.9%. The
asymmetric counts are printed whether or not they are zero.

**Report db time next to query count.** Finding 39 is the reason: on the write
path a change can be worth -31% wall clock and -13% queries, and query count
alone undervalues it. On the read path the two track each other (finding 38).
Neither is a substitute for the other, so both are always shown.

Not a gate. `compare.py` gates Tier 1 on regressions; this reports movement in
both directions and fails only when the comparison itself is not valid -- no
overlap, or the two paths being the same run.

    python3 perf/compare_screen.py \\
        --baseline perf/results/screen-writes-dc-next.json \\
        --current perf/results/screen-writes-dc.json \\
        --json-out perf/results/diff-screen-writes-dc.json
"""

import argparse
import hashlib
import json
import sys

METRICS = (("query_count", "queries"), ("wall_ms", "wall_ms"), ("db_ms", "db_ms"))


def load(path):
    """(kind, {(id, kind): record}) for one screen run.

    The shape is detected, not flagged: the read screen writes its records under
    `endpoints` and the write screen under `measurements`, and no run carries
    both. A flag here would be one more thing to get wrong at the call site.
    """
    with open(path) as fh:
        data = json.load(fh)
    if "measurements" in data:
        kind, records = "write", data["measurements"]
    elif "endpoints" in data:
        kind, records = "read", data["endpoints"]
    else:
        raise SystemExit(f"{path}: neither 'measurements' nor 'endpoints' -- not a screen run")
    return kind, {(r["id"], r.get("kind")): r for r in records}


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def usable(record):
    """Whether a measurement is comparable at all.

    A 4xx read or a rejected write payload is a coverage fact, not a cost, and
    averaging one into an aggregate moves the total by whatever the error path
    happened to cost. Records the screen skipped outright have no query_count.
    """
    status = record.get("status")
    return status is not None and status < 300 and record.get("query_count") is not None


def group_of(record):
    """The row label: the model for a write screen, the view name for a read one."""
    return record.get("model") or record["id"]


def delta_block(base_total, curr_total):
    return {
        "baseline": round(base_total, 2),
        "current": round(curr_total, 2),
        "delta": round(curr_total - base_total, 2),
        "pct": round((curr_total - base_total) / base_total * 100.0, 2) if base_total else 0.0,
    }


def aggregate(rows, base, curr):
    """Totals and percentage movement for every metric over `rows`."""
    out = {}
    for field, label in METRICS:
        out[label] = delta_block(sum(base[k][field] for k in rows), sum(curr[k][field] for k in rows))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="the before arm")
    ap.add_argument("--current", required=True, help="the after arm")
    ap.add_argument("--json-out")
    ap.add_argument("--top", type=int, default=20, help="rows to print per table; the JSON always holds all of them")
    args = ap.parse_args()

    base_kind, base = load(args.baseline)
    curr_kind, curr = load(args.current)
    if base_kind != curr_kind:
        raise SystemExit(f"cannot compare a {base_kind} screen against a {curr_kind} screen")

    invalid = []
    if args.baseline == args.current or digest(args.baseline) == digest(args.current):
        invalid.append("baseline and current are the same run")

    matched = sorted(set(base) & set(curr))
    only_baseline = [{"id": i, "kind": k} for i, k in sorted(set(base) - set(curr))]
    only_current = [{"id": i, "kind": k} for i, k in sorted(set(curr) - set(base))]

    comparable, excluded = [], []
    for key in matched:
        bad = [side for side, rec in (("baseline", base[key]), ("current", curr[key])) if not usable(rec)]
        if bad:
            excluded.append(
                {
                    "id": key[0],
                    "kind": key[1],
                    "failed_on": bad,
                    "reason": (curr[key].get("reason") or base[key].get("reason") or "").strip()[:200] or None,
                    "status": [base[key].get("status"), curr[key].get("status")],
                }
            )
        else:
            comparable.append(key)

    if not comparable:
        invalid.append("no measurement is present and successful on both sides")

    coverage = {
        "baseline_records": len(base),
        "current_records": len(curr),
        "matched": len(matched),
        "only_in_baseline": len(only_baseline),
        "only_in_current": len(only_current),
        "excluded_failed": len(excluded),
        "comparable": len(comparable),
        "identical_coverage": not only_baseline and not only_current,
    }

    totals = aggregate(comparable, base, curr) if comparable else {}

    improved = [k for k in comparable if curr[k]["query_count"] < base[k]["query_count"]]
    worse = [k for k in comparable if curr[k]["query_count"] > base[k]["query_count"]]
    movement = {
        "improved": len(improved),
        "unchanged": len(comparable) - len(improved) - len(worse),
        "worse": len(worse),
        "unstable_query_count": sum(1 for k in comparable if curr[k].get("query_count_stable") is False),
    }

    rows = []
    for key in comparable:
        b, c = base[key], curr[key]
        row = {"id": key[0], "kind": key[1], "model": group_of(c)}
        for field, label in METRICS:
            row[label] = delta_block(b[field], c[field])
        rows.append(row)
    rows.sort(key=lambda r: (-abs(r["queries"]["delta"]), r["id"]))

    groups = {}
    for key in comparable:
        groups.setdefault(group_of(curr[key]), []).append(key)
    group_rows = []
    for name, keys in groups.items():
        row = {"model": name, "measurements": len(keys)}
        row.update(aggregate(keys, base, curr))
        group_rows.append(row)
    group_rows.sort(key=lambda r: (-abs(r["queries"]["delta"]), r["model"]))

    print(f"{base_kind} screen: {args.baseline} -> {args.current}")
    print(
        f"\nCOVERAGE  {coverage['baseline_records']} baseline / {coverage['current_records']} current records, "
        f"{coverage['matched']} matched, {coverage['comparable']} comparable"
    )
    print(
        f"  only in baseline {coverage['only_in_baseline']}, only in current {coverage['only_in_current']}, "
        f"excluded as failed {coverage['excluded_failed']}"
    )
    one_sided = [("baseline", e) for e in only_baseline] + [("current", e) for e in only_current]
    for side, entry in one_sided[:15]:
        print(f"  only in {side}: {entry['id']} {entry['kind']}")
    if len(one_sided) > 15:
        print(f"  ... and {len(one_sided) - 15} more one-sided")

    def show(title, table, label):
        if not table:
            return
        print(f"\n{title} ({len(table)}, showing {min(args.top, len(table))})")
        for r in table[: args.top]:
            print(
                f"  {r['queries']['delta']:+7.0f} q  {r['queries']['pct']:+7.2f}%  "
                f"{r['db_ms']['delta']:+9.1f} db ms  {r['wall_ms']['delta']:+9.1f} wall ms  {label(r)}"
            )

    show("BY MODEL", group_rows, lambda r: f"{r['model']} ({r['measurements']})")
    show("BY MEASUREMENT", rows, lambda r: f"{r['id']} {r['kind']}")

    if comparable:
        print(f"\nTOTALS over {len(comparable)} comparable measurements")
        for _, label in METRICS:
            t = totals[label]
            print(f"  {label:11s} {t['baseline']:>12,.1f} -> {t['current']:>12,.1f}  ({t['pct']:+.2f}%)")
        print(
            f"  query count: {movement['improved']} improved, {movement['unchanged']} unchanged, "
            f"{movement['worse']} worse"
        )
        if movement["unstable_query_count"]:
            print(f"  {movement['unstable_query_count']} measurement(s) had an unstable query count on the current arm")

    payload = {
        "schema": 1,
        "screen": base_kind,
        "baseline": args.baseline,
        "current": args.current,
        "coverage": coverage,
        "totals": totals,
        "query_movement": movement,
        "invalid": invalid,
        "only_in_baseline": only_baseline,
        "only_in_current": only_current,
        "excluded": excluded,
        "models": group_rows,
        "measurements": rows,
    }
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)

    # Written before the exit, deliberately: an invalid comparison is the one a
    # reader most needs the numbers for, to see how it went wrong.
    if invalid:
        for reason in invalid:
            print(f"\nINVALID: {reason}")
        return 2
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
