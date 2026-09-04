#!/usr/bin/env python
"""Upper bound on what dropping extras_objectchange's indexes could ever buy.

Finding 24: extras_objectchange carries eleven indexes beyond its primary key,
every one maintained on every insert, on the highest-insert-volume table in
Nautobot. Whether that is worth pursuing is a question with a ceiling, and the
ceiling is cheap to measure -- drop them all, measure, and see.

This is deliberately not a proposal to drop anything. It measures the best case
so the design work is only done if the best case justifies it, the same way
finding 27 was declined at a measured 3.1%.

    perf/dc.sh exec -T nautobot python /source/perf/probe_index_cost.py --arm with
    perf/dc.sh exec -T nautobot python /source/perf/probe_index_cost.py --arm without
"""

import argparse
import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier1w_writes as t1w  # noqa: E402

# Non-unique, non-primary only. A unique index backs a constraint -- Postgres
# refuses to drop it directly, and dropping the constraint would remove a
# uniqueness guarantee, which is a behaviour change rather than an index tidy-up.
# What is left is the set that exists purely to make reads faster, which is the
# set whose insert-side cost is worth knowing.
LIST_SQL = """
SELECT c.relname
  FROM pg_index x
  JOIN pg_class c ON c.oid = x.indexrelid
  JOIN pg_class t ON t.oid = x.indrelid
 WHERE t.relname = 'extras_objectchange'
   AND NOT x.indisprimary
   AND NOT x.indisunique
 ORDER BY c.relname
"""


def index_names():
    with connection.cursor() as cursor:
        cursor.execute(LIST_SQL)
        return [row[0] for row in cursor.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=("with", "without"), required=True)
    ap.add_argument("--op", default=f"bulk.create.interfaces.x{t1w.BULK_N}.deferred")
    args = ap.parse_args()

    names = index_names()
    if args.arm == "without":
        # DROP INDEX, not a migration. The caller restores the snapshot
        # afterwards, which puts them back.
        with connection.cursor() as cursor:
            for name in names:
                cursor.execute(f'DROP INDEX IF EXISTS "{name}"')
        remaining = index_names()
        print(f"dropped {len(names) - len(remaining)} of {len(names)} indexes", file=sys.stderr)
    else:
        print(f"{len(names)} indexes present", file=sys.stderr)

    user_model = get_user_model()
    user = user_model.objects.filter(username=t1w.PERF_USER).first()
    if user is None:
        user = user_model.objects.create(username=t1w.PERF_USER, is_superuser=True, is_staff=True, is_active=True)
    fixtures = t1w.fixtures()
    ops = dict(t1w.OPERATIONS)
    rec = t1w.measure(user, args.op, ops[args.op], fixtures)
    print(f"RESULT {args.arm} {rec['wall_ms']:.1f} {rec['query_count']} {rec['db_ms']:.1f}")


if __name__ == "__main__":
    main()
