#!/usr/bin/env python
"""Measure what Tier 1W's rolled-back transactions leave out.

tier1w_writes.py runs each operation inside `transaction.atomic()` and raises to
unwind it, which buys identical starting state -- the bulk triple depends on it,
since all three create the same 100 rows with the same names -- at the cost of
never committing. No WAL flush, no fsync, and `transaction.on_commit` callbacks
never fire.

Whether that matters is a question with a number, so this measures it: one arm
per invocation, with a snapshot restore between arms so each starts from
byte-identical state.

One arm per process is not a stylistic choice. A restore stops the container this
runs in, so the alternation has to be orchestrated from outside -- which is
exactly the isolation model the write screening matrix needs. The "20-second
restore" this comment used to cite was never measured: restore_snapshot.sh is 49
seconds, and perf/reset_db.sh does the same job in 1.3 by cloning a template.
See finding 33.

    perf/dc.sh exec -T nautobot python /source/perf/probe_commit_cost.py --arm rollback
"""

import argparse
import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier1w_writes as t1w  # noqa: E402

from nautobot.extras.context_managers import web_request_context  # noqa: E402


def run(user, fn, fixtures, commit):
    """Run one operation, committing or unwinding, and report wall clock and queries."""
    with CaptureQueriesContext(connection) as ctx:
        start = time.perf_counter()
        if commit:
            with web_request_context(user, context_detail="perf-commit-probe"):
                fn(fixtures)
        else:
            try:
                with transaction.atomic():
                    with web_request_context(user, context_detail="perf-commit-probe"):
                        fn(fixtures)
                    raise t1w.Rollback
            except t1w.Rollback:
                pass
        elapsed = (time.perf_counter() - start) * 1000.0
    return elapsed, len(ctx.captured_queries)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", default=f"bulk.create.interfaces.x{t1w.BULK_N}.deferred")
    ap.add_argument("--arm", choices=("commit", "rollback"), required=True)
    args = ap.parse_args()

    ops = dict(t1w.OPERATIONS)
    if args.op not in ops:
        sys.exit(f"unknown op {args.op}; choose from:\n  " + "\n  ".join(ops))
    fn = ops[args.op]

    user_model = get_user_model()
    user = user_model.objects.filter(username=t1w.PERF_USER).first()
    if user is None:
        user = user_model.objects.create(username=t1w.PERF_USER, is_superuser=True, is_staff=True, is_active=True)
    fixtures = t1w.fixtures()

    ms, queries = run(user, fn, fixtures, commit=(args.arm == "commit"))
    print(f"RESULT {args.op} {args.arm} {ms:.1f} {queries}")


if __name__ == "__main__":
    main()
