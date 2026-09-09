#!/usr/bin/env python
"""Count SQL compilations, not just SQL executions, per workload scenario.

Every deterministic gate on this branch so far counts queries *executed* -- the
execute_wrapper counter, compare.py's query_count, Redis reads. That is blind to a whole
class of waste: Django builds and compiles a query, then discards it without executing it.
Iterating `queryset.none()` does exactly that. Measured standalone: 604us for Location and
973us for Device, with **zero** SQL executed, against 7.4us for the `.none()` clone alone.

`APISelect` populates its options from the API at runtime, so a filter-form field that
bound no data renders an empty `<select>` -- and `DynamicModelChoiceField.get_bound_field()`
expresses "no data" as `queryset.none()`. The device list's filter form renders 19 such
widgets and spent 9.7ms compiling queries that return nothing.

An experiment that removes discarded compilations therefore changes no query count at all,
and would have no deterministic counter to gate on. This is that counter:
`SQLCompiler.as_sql()` calls, which happen whether or not the compiled query is ever run.

Reports per scenario: total wall (median and min of PERF_REPS reps), SQL executed, SQL
compiled, the difference between them, and a body digest. Compilations and executions are
deterministic; the ms figures are not.

    perf/scripts/dc.sh exec -T -e PERF_REPS=9 nautobot \
        python /source/perf/scripts/probe_query_compilations.py [scenario-id ...]
"""

import hashlib
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.db.models.sql.compiler import SQLCompiler  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402
import workload as workload_mod  # noqa: E402

REPS = int(os.environ.get("PERF_REPS", "9"))

DEFAULT_SCENARIOS = [
    # filter-form bearing document requests
    "ui.device.list",
    "ui.circuit.list",
    "ui.prefix.list",
    "ui.ipaddress.list",
    "ui.location.list",
    "ui.vlan.list",
    # controls: no filter form rendered
    "ui.device.list.rows",
    "ui.device.detail",
    "ui.home",
]


class ExecutionCounter:
    """DEBUG is False on the perf stack, so connection.queries stays empty."""

    def __init__(self):
        self.n = 0

    def __call__(self, execute, sql, params, many, context):
        self.n += 1
        return execute(sql, params, many, context)


class CompilationCounter:
    """Count SQLCompiler.as_sql calls, executed or not.

    Subclasses of SQLCompiler (SQLInsertCompiler, SQLAggregateCompiler, ...) define their
    own as_sql, so patching the base class counts only the SELECT path. That is the path
    `.none()` iteration takes, which is what this exists to see.
    """

    def __init__(self):
        self.n = 0
        self._original = SQLCompiler.as_sql

    def __enter__(self):
        counter = self
        original = self._original

        def as_sql(compiler_self, *args, **kwargs):
            counter.n += 1
            return original(compiler_self, *args, **kwargs)

        SQLCompiler.as_sql = as_sql
        return self

    def __exit__(self, *exc):
        SQLCompiler.as_sql = self._original
        return False


def main():
    ids = sys.argv[1:] or DEFAULT_SCENARIOS
    resolved, _ = workload_mod.resolve(workload_mod.DEFAULT_WORKLOAD)
    rows = {r["id"]: r for r in resolved}
    missing = [i for i in ids if i not in rows]
    if missing:
        sys.exit(f"unknown scenario(s): {', '.join(missing)}")

    client = get_perf_client()
    print(f"reps={REPS}  (one untimed warm-up discarded per scenario)\n")
    print(f"{'scenario':26s} {'executed':>9} {'compiled':>9} {'discarded':>10} {'median':>9} {'min':>8}  digest")

    for sid in ids:
        row = rows[sid]
        url, headers = row["url"], row.get("headers") or {}
        client.get(url, headers=headers)  # warm-up, discarded

        execs = ExecutionCounter()
        with CompilationCounter() as compiles, connection.execute_wrapper(execs):
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            sys.exit(f"NON-200 {resp.status_code} for {sid} -- measurement void")

        digests = {hashlib.sha256(resp.content).hexdigest()[:16]}
        timings = []
        for _ in range(REPS):
            start = time.perf_counter()
            r = client.get(url, headers=headers)
            timings.append((time.perf_counter() - start) * 1000.0)
            digests.add(hashlib.sha256(r.content).hexdigest()[:16])

        digest = sorted(digests)[0] + ("" if len(digests) == 1 else f" UNSTABLE({len(digests)})")
        print(
            f"{sid:26s} {execs.n:9d} {compiles.n:9d} {compiles.n - execs.n:10d} "
            f"{statistics.median(timings):7.1f}ms {min(timings):6.1f}ms  {digest}"
        )


if __name__ == "__main__":
    main()
