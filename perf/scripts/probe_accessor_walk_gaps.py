#!/usr/bin/env python
"""Which table columns lose their queryset optimization to the accessor walk's FK guard?

`BaseTable.__init__` walks each visible column's accessor and builds `select_related` and
`prefetch_related` paths. The to-many and GenericForeignKey branches are guarded with
`and not select_path`, so once a ForeignKey segment has been seen, any to-many segment after it is
dropped: `interface__ip_addresses` optimizes as `select_related("interface")` and the
`ip_addresses` hop silently becomes a per-row read.

This enumerates every registered table class and reports the columns where that happens, so the
question "does that guard cost anything on this tree" is answered by a list rather than by reading
the code.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_accessor_walk_gaps.py
"""

import contextlib
import importlib
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.contenttypes.fields import GenericForeignKey  # noqa: E402
from django.core.exceptions import FieldDoesNotExist  # noqa: E402
from django.db.models import ManyToOneRel  # noqa: E402
from django.db.models.fields.related import ForeignKey, OneToOneField, RelatedField  # noqa: E402

from nautobot.core.tables import BaseTable  # noqa: E402


def walk(model, accessor):
    """Replay BaseTable's accessor walk, reporting what the FK guard drops."""
    column_model, select_path, dropped = model, [], []
    for field_name in str(accessor).split("__"):
        try:
            field = column_model._meta.get_field(field_name)
        except (FieldDoesNotExist, AttributeError):
            break
        if isinstance(field, (ForeignKey, OneToOneField)):
            select_path.append(field_name)
            column_model = field.remote_field.model
        elif isinstance(field, (RelatedField, ManyToOneRel, GenericForeignKey)):
            if select_path:
                dropped.append("__".join([*select_path, field_name]))
            break
        else:
            break
    return dropped


def main():
    # Importing the view modules is what registers the table classes; without this the subclass
    # walk finds almost nothing and the scan reports a confident, wrong "no hits".
    for app in ("circuits", "cloud", "dcim", "extras", "ipam", "tenancy", "virtualization", "wireless"):
        with contextlib.suppress(ImportError):
            importlib.import_module(f"nautobot.{app}.tables")

    seen = set()
    hits = []
    for subclass in all_subclasses(BaseTable):
        meta = getattr(subclass, "Meta", None)
        model = getattr(meta, "model", None)
        if model is None or subclass.__name__ in seen:
            continue
        seen.add(subclass.__name__)
        for name, column in subclass.base_columns.items():
            accessor = column.accessor or name
            for dropped in walk(model, accessor):
                hits.append((subclass.__name__, name, str(accessor), dropped))

    print(f"  {len(seen)} table classes inspected")
    for table, column, accessor, dropped in sorted(hits):
        print(f"  {table:34s} column={column:24s} accessor={accessor:36s} dropped prefetch: {dropped}")
    if not hits:
        print("  no column loses a prefetch to the FK guard")


def all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from all_subclasses(sub)


if __name__ == "__main__":
    main()
