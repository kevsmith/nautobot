"""Database cost of REST endpoints under load, from Postgres's side.

For each URL and concurrency level: reset pg_stat_statements, drive the URL through uwsgi with
cassowary, then read what the server did -- statements, execution and planning time, rows and
shared-buffer blocks -- alongside cassowary's throughput and latency. Run on the host, from the
repo root, inside measure.sh.

    perf/scripts/measure.sh /usr/bin/python3 perf/scripts/probe_db_load.py --out results.json

Needs `pg_stat_statements` loaded; turn `pg_stat_statements.track_planning` on for planning time.
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "perf/scripts")
from tier2_latency import cassowary_once, probe

URLS = [
    "/api/dcim/devices/?limit=100&depth=1",
    "/api/dcim/devices/?limit=100&depth=2",
    "/api/dcim/devices/?limit=100&depth=3",
    "/api/ipam/ip-addresses/?limit=100&depth=2",
    "/api/ipam/prefixes/?limit=100&depth=2",
    "/api/dcim/interfaces/?limit=100&depth=2",
]
STATS_SQL = """
select coalesce(sum(calls), 0), coalesce(sum(total_exec_time), 0), coalesce(sum(total_plan_time), 0),
       coalesce(sum(rows), 0), coalesce(sum(shared_blks_hit), 0), coalesce(sum(shared_blks_read), 0),
       coalesce(sum(plans), 0)
from pg_stat_statements s join pg_database d on d.oid = s.dbid
where d.datname = 'nautobot' and s.query not ilike '%pg_stat_statements%'
"""


def psql(sql):
    # Fixed argv, run from the repo root on the measurement host; docker is found on PATH as measure.sh's are.
    db = subprocess.run(
        ["perf/scripts/dc.sh", "ps", "-q", "db"],  # noqa: S607
        capture_output=True,
        text=True,
    ).stdout.strip()
    proc = subprocess.run(  # noqa: S603
        ["docker", "exec", "-i", db, "sh", "-c", 'psql -U "$POSTGRES_USER" -d nautobot -At -F "|"'],  # noqa: S607
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--base-url", default="http://localhost:8180")
ap.add_argument("--concurrency", default="1,3,6")
ap.add_argument("--requests-per-worker", type=int, default=12)
args = ap.parse_args()

headers = [f"Authorization: Token {os.environ['NAUTOBOT_TOKEN']}", "Accept: application/json"]
records = []
for url in URLS:
    full = args.base_url + url
    status, size, _ = probe(full, headers, 120)
    if status != 200:
        print(f"!! {url} probe status {status}", flush=True)
        continue
    for c in [int(x) for x in args.concurrency.split(",")]:
        n = max(c * args.requests_per_worker, 12)
        psql("select pg_stat_statements_reset();")
        t0 = time.perf_counter()
        m = cassowary_once(full, headers, n, c, 120)
        elapsed = time.perf_counter() - t0
        calls, exec_ms, plan_ms, rows, hit, read, plans = (float(x) for x in psql(STATS_SQL).split("|"))
        sp = m.get("server_processing", {}) or {}
        rec = {
            "url": url,
            "concurrency": c,
            "requests": m.get("total_requests"),
            "failed": m.get("failed_requests"),
            "elapsed_s": round(elapsed, 2),
            "rps": m.get("requests_per_second"),
            "latency_ms_median": sp.get("median"),
            "latency_ms_p95": sp.get("95th_percentile"),
            "bytes": size,
            "db_statements": int(calls),
            "db_exec_ms": round(exec_ms, 1),
            "db_plan_ms": round(plan_ms, 1),
            "db_rows": int(rows),
            "db_blocks_hit": int(hit),
            "db_blocks_read": int(read),
            # With track_planning on, `plans` < `statements` means plans were reused -- prepared statements.
            "db_plans": int(plans),
        }
        req = rec["requests"] or n
        rec["per_request"] = {
            "statements": round(calls / req, 1),
            "exec_ms": round(exec_ms / req, 2),
            "plan_ms": round(plan_ms / req, 2),
            "rows": round(rows / req, 1),
            "blocks": round((hit + read) / req, 1),
        }
        rec["db_busy_share"] = round((exec_ms + plan_ms) / 1000.0 / elapsed, 4)
        records.append(rec)
        print(json.dumps(rec), flush=True)

with open(args.out, "w") as fh:
    json.dump(records, fh, indent=1)
