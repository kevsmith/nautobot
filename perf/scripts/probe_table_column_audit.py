#!/usr/bin/env python
"""Per-column N+1 audit across every registered table, using BaseTable(columns=[...]).

Finding 72 added the `columns=` kwarg because queryset optimization keys off *visible* columns, so
a column can only be priced by rendering it alone. This does that for every column of every table
whose model has enough rows to show a slope: render the column over N rows and then 2N, and report
the per-row query cost.

Cost is per column, not per page: a column at 1.0 q/row costs 100 queries on a 100-row page
wherever it is visible, and nothing where it is not.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_table_column_audit.py
    PERF_MIN_ROWS=20 PERF_SLOPE=0.1 ... (defaults below)
"""

import contextlib
import importlib
import json
import os
import sys
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

from nautobot.core.tables import BaseTable  # noqa: E402

MIN_ROWS = int(os.environ.get("PERF_MIN_ROWS", "20"))
SLOPE = float(os.environ.get("PERF_SLOPE", "0.1"))
APPS = ("circuits", "cloud", "dcim", "extras", "ipam", "tenancy", "virtualization", "wireless", "load_balancers")


def all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from all_subclasses(sub)


def render(table_class, model, pks, column):
    queryset = model.objects.filter(pk__in=pks)
    with CaptureQueriesContext(connection) as ctx:
        table = table_class(data=queryset, columns=[column])
        for row in table.rows:
            for _ in row.items():
                pass
    return len(ctx.captured_queries)


def main():
    get_perf_client()  # some columns read request-scoped state; keep the environment identical to a page
    for app in APPS:
        with contextlib.suppress(ImportError):
            importlib.import_module(f"nautobot.{app}.tables")

    seen, offenders, skipped = set(), [], 0
    for table_class in all_subclasses(BaseTable):
        model = getattr(getattr(table_class, "Meta", None), "model", None)
        if model is None or table_class.__name__ in seen:
            continue
        seen.add(table_class.__name__)
        pks = list(model.objects.values_list("pk", flat=True)[: MIN_ROWS * 2])
        if len(pks) < MIN_ROWS * 2:
            skipped += 1
            continue
        half, full = pks[:MIN_ROWS], pks
        for column in list(table_class.base_columns):
            try:
                render(table_class, model, half, column)  # discarded: process caches are cold once
                low = render(table_class, model, half, column)
                high = render(table_class, model, full, column)
            except Exception:  # noqa: BLE001  # a column that cannot render alone is not an offender
                if os.environ.get("PERF_TRACE"):
                    traceback.print_exc()
                continue
            slope = (high - low) / MIN_ROWS
            if slope >= SLOPE:
                offenders.append(
                    {"table": table_class.__name__, "column": column, "slope": round(slope, 3), "low": low, "high": high}
                )

    offenders.sort(key=lambda o: -o["slope"])
    print(f"  {len(seen)} tables, {skipped} skipped for fewer than {MIN_ROWS * 2} rows; "
          f"{len(offenders)} columns at >= {SLOPE} q/row")
    for o in offenders:
        print(f"  {o['slope']:6.3f} q/row  {o['table']:38s} {o['column']:28s} {o['low']:>4} -> {o['high']:<4}")
    if os.environ.get("PERF_JSON"):
        with open(os.environ["PERF_JSON"], "w") as fh:
            json.dump(offenders, fh, indent=2)


if __name__ == "__main__":
    main()
