#!/usr/bin/env python
"""Attribute a table's per-cell cost across column classes, and split accessor from render.

Finding 50 measured ~0.6ms to render one table cell on ui.device.list.rows -- 1,000 <td>,
inc/table.html 479.9ms exclusive plus 500 TemplateColumn string renders at 119.4ms -- but
attributed it to a template that is 105 lines of ordinary markup. The cost is django-tables2
work landing on whichever template is executing, and this says which work.

django-tables2 3.0.1 renders a cell as:

    BoundRow.get_cell(name)
      -> _get_and_render_with()    Accessor resolution, get_FOO_display handling
           -> _call_render()       bound_column.render(), then bound_column.link() if linkified

So get_cell minus _call_render is accessor and dispatch; _call_render is render plus linkify.
Grouping by column class answers the question reputation cannot: whether the cost is
`linkify=True` reversing a URL per cell, Nautobot's caching TemplateColumn (findings 07 and
11) still costing something per cell, or base BoundColumn machinery.

DeviceTable has eight `linkify=True` columns and three TemplateColumns, so the two candidates
are separable by construction.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_table_columns.py
"""

import argparse
import collections
import json
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django_tables2.rows import BoundRow  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402


class CellTimer:
    def __init__(self):
        self.cells = collections.defaultdict(
            lambda: {"n": 0, "total_ms": 0.0, "render_ms": 0.0, "linkify": None, "cls": None})
        self._inner = 0.0

    def install(self):
        self._get_cell = BoundRow.get_cell
        self._call_render = BoundRow._call_render
        t = self

        def get_cell(row_self, name):
            col = row_self.table.columns[name]
            rec = t.cells[name]
            if rec["cls"] is None:
                rec["cls"] = type(col.column).__name__
                # `link` is truthy only when the column linkifies, so this is the
                # per-column answer to "does this cell resolve a URL".
                try:
                    rec["linkify"] = bool(col.link)
                except Exception:
                    rec["linkify"] = None
            saved, t._inner = t._inner, 0.0
            start = time.perf_counter()
            try:
                return t._get_cell(row_self, name)
            finally:
                total = (time.perf_counter() - start) * 1000.0
                rec["n"] += 1
                rec["total_ms"] += total
                rec["render_ms"] += t._inner
                t._inner = saved

        def call_render(row_self, bound_column, value=None):
            start = time.perf_counter()
            try:
                return t._call_render(row_self, bound_column, value)
            finally:
                t._inner += (time.perf_counter() - start) * 1000.0

        BoundRow.get_cell = get_cell
        BoundRow._call_render = call_render

    def remove(self):
        BoundRow.get_cell = self._get_cell
        BoundRow._call_render = self._call_render


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="/dcim/devices/")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_perf_client()
    client.get(args.url, HTTP_HX_REQUEST="true")  # discard: cold process caches

    runs = []
    for _ in range(args.reps):
        t = CellTimer()
        t.install()
        try:
            start = time.perf_counter()
            client.get(args.url, HTTP_HX_REQUEST="true")
            wall = (time.perf_counter() - start) * 1000.0
        finally:
            t.remove()
        runs.append((wall, {k: dict(v) for k, v in t.cells.items()}))

    wall = statistics.median([r[0] for r in runs])
    names = sorted({k for _, c in runs for k in c})
    rows = []
    for name in names:
        recs = [c[name] for _, c in runs if name in c]
        rows.append({
            "column": name,
            "cls": recs[0]["cls"],
            "linkify": recs[0]["linkify"],
            "cells": int(statistics.median([r["n"] for r in recs])),
            "total_ms": round(statistics.median([r["total_ms"] for r in recs]), 1),
            "render_ms": round(statistics.median([r["render_ms"] for r in recs]), 1),
        })
    for r in rows:
        r["accessor_ms"] = round(r["total_ms"] - r["render_ms"], 1)
        r["ms_per_cell"] = round(r["total_ms"] / r["cells"], 3) if r["cells"] else 0.0
    rows.sort(key=lambda r: -r["total_ms"])

    if args.json:
        print(json.dumps({"wall_ms": round(wall, 1), "columns": rows}, indent=2))
        return

    tot = sum(r["total_ms"] for r in rows)
    print(f"  wall {wall:.1f}ms   cell rendering {tot:.1f}ms ({tot / wall * 100:.0f}% of the request)")
    print(f"  {'column':34s} {'class':22s} link {'cells':>5} {'total':>8} {'render':>8} {'accessor':>9} {'ms/cell':>8}")
    for r in rows:
        print(f"  {r['column']:34s} {r['cls']:22s} {str(r['linkify'])[:5]:5s} {r['cells']:>5} "
              f"{r['total_ms']:>8.1f} {r['render_ms']:>8.1f} {r['accessor_ms']:>9.1f} {r['ms_per_cell']:>8.3f}")
    by_cls = collections.defaultdict(float)
    by_link = collections.defaultdict(float)
    for r in rows:
        by_cls[r["cls"]] += r["total_ms"]
        by_link[bool(r["linkify"])] += r["total_ms"]
    print("  --- by column class ---")
    for cls, ms in sorted(by_cls.items(), key=lambda kv: -kv[1]):
        print(f"    {ms:>8.1f}ms  {cls}")
    print("  --- by linkify ---")
    for lk, ms in sorted(by_link.items(), key=lambda kv: -kv[1]):
        print(f"    {ms:>8.1f}ms  linkify={lk}")


if __name__ == "__main__":
    main()
