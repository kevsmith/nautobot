#!/usr/bin/env python
"""Query shapes for one table column rendered alone, to name what a per-row cost actually reads.

Companion to `probe_table_column_audit.py`: the audit says which columns scale, this says why.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_column_shapes.py \
        nautobot.dcim.tables.InterfaceTable mtu
"""

import collections
import importlib
import os
import re
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

ROWS = int(os.environ.get("PERF_ROWS", "20"))


def normalize(sql):
    sql = re.sub(r"'[^']*'", "?", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    sql = re.sub(r"IN \([^)]*\)", "IN (?)", sql)
    return " ".join(sql.split())[:104]


def main():
    dotted, column = sys.argv[1], sys.argv[2]
    module_name, class_name = dotted.rsplit(".", 1)
    table_class = getattr(importlib.import_module(module_name), class_name)
    model = table_class.Meta.model
    get_perf_client()
    pks = list(model.objects.values_list("pk", flat=True)[:ROWS])

    def render():
        with CaptureQueriesContext(connection) as ctx:
            table = table_class(data=model.objects.filter(pk__in=pks), columns=[column])
            for row in table.rows:
                for _ in row.items():
                    pass
        return ctx.captured_queries

    render()  # discarded
    queries = render()
    shapes = collections.Counter(normalize(q["sql"]) for q in queries)
    print(f"  {class_name}.{column}: {len(pks)} rows, {len(queries)} queries")
    for sql, n in shapes.most_common(6):
        print(f"      x{n:<4} {sql}")


if __name__ == "__main__":
    main()
