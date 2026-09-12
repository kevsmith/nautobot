#!/usr/bin/env python
"""Query cost of one table column, measured with that column alone made visible.

Queryset optimization happens in `BaseTable.__init__` and depends on which columns are visible, so
a column hidden by default is never optimized and never measured. `BaseTable(columns=[...])` shows
exactly the named columns, which is what makes a hidden column's per-row cost visible at all.

Renders the table to a string, which is what forces every cell to evaluate.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_column_isolated.py \
        nautobot.dcim.tables.InterfaceRedundancyGroupAssociationTable interface__ip_addresses
"""

import importlib
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402


def main():
    dotted, column = sys.argv[1], sys.argv[2]
    limit = int(os.environ.get("PERF_ROWS", "20"))
    module_name, class_name = dotted.rsplit(".", 1)
    table_class = getattr(importlib.import_module(module_name), class_name)
    model = table_class.Meta.model

    def render(n):
        queryset = model.objects.all()[:n]
        pks = [o.pk for o in queryset]
        with CaptureQueriesContext(connection) as ctx:
            table = table_class(data=model.objects.filter(pk__in=pks), columns=[column])
            cells = sum(1 for row in table.rows for _ in row.items())
        return cells, len(ctx.captured_queries)

    # One discarded render: process-level caches (content types, config) are cold once only, and
    # without this the first measurement carries their cost and the second looks artificially cheap.
    render(limit)

    for n in (limit // 2, limit):
        cells, queries = render(n)
        print(f"  {class_name}.{column}: {n:>3} rows requested, {cells:>4} cells rendered, {queries:>4} queries")


if __name__ == "__main__":
    main()
