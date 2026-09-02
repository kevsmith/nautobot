#!/usr/bin/env python
"""Tier 1: deterministic per-endpoint query profiling.

Drives every endpoint in an ``endpoints.yml`` (from ``nautobot-server
generate_performance_test_endpoints``) through the Django test client as an
authenticated superuser, recording the SQL each view emits.

Query counts are *deterministic* -- unlike wall-clock timings they do not move
with machine load -- which is what makes them usable as a regression gate at
small dataset sizes.

Run inside the nautobot container:

    nautobot-server shell < /dev/null  # (not needed; this bootstraps itself)
    python /source/perf/tier1_queries.py --endpoints /source/perf/endpoints.yml \
        --out /source/perf/results/tier1-baseline.json
"""

import argparse
from collections import Counter
import hashlib
import json
import os
import re
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test.client import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
import workload as workload_mod  # noqa: E402

PERF_USER = "perfbot"

# SQL literal normalisation, so that the same query shape with different bound
# values collapses to one key and repeats become visible.
_RE_STRING = re.compile(r"'[^']*'")
_RE_NUMBER = re.compile(r"\b\d+\b")
_RE_IN_LIST = re.compile(r"IN \((?:\s*\?\s*,)*\s*\?\s*\)", re.IGNORECASE)
_RE_WS = re.compile(r"\s+")


def normalize_sql(sql):
    """Collapse a SQL string to its shape, discarding bound literals."""
    sql = _RE_STRING.sub("?", sql)
    sql = _RE_NUMBER.sub("?", sql)
    sql = _RE_IN_LIST.sub("IN (?)", sql)
    return _RE_WS.sub(" ", sql).strip()


# Volatile markers that legitimately differ between two identical requests.
_RE_CSRF = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]*"')
_RE_NONCE = re.compile(rb'nonce="[^"]*"')


def fingerprint(body, content_type):
    """Return a content hash, plus a separate hash of result *ordering*.

    Byte length is a poor equivalence check: a queryset change can reorder rows
    without changing the response size at all. For JSON we hash the canonical
    form and, separately, the sequence of result identities so a pure reorder is
    caught on its own. For HTML we strip the known-volatile tokens and hash.
    """
    order_hash = None
    if "json" in (content_type or ""):
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            return hashlib.sha256(body).hexdigest()[:16], None
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        results = parsed.get("results") if isinstance(parsed, dict) else None
        if isinstance(results, list):
            ids = [str(r.get("id", "")) if isinstance(r, dict) else str(r) for r in results]
            order_hash = hashlib.sha256("|".join(ids).encode()).hexdigest()[:16]
        return content_hash, order_hash

    normalized = _RE_CSRF.sub(b"", _RE_NONCE.sub(b"", body))
    return hashlib.sha256(normalized).hexdigest()[:16], None


def get_perf_client():
    """Return a test client logged in as a superuser."""
    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=PERF_USER,
        defaults={"is_superuser": True, "is_staff": True, "is_active": True},
    )
    if created or not user.is_superuser:
        user.is_superuser = True
        user.is_staff = True
        user.is_active = True
        user.save()
    # DEBUG=False means ALLOWED_HOSTS is enforced, and the test client's default
    # Host of 'testserver' is rejected. ALLOWED_HOSTS carries '.localhost'.
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client


def measure(client, url):
    """GET ``url`` once, returning its query profile."""
    with CaptureQueriesContext(connection) as ctx:
        start = time.perf_counter()
        try:
            response = client.get(url, follow=False)
            status = response.status_code
            body = response.content if hasattr(response, "content") else b""
            size = len(body)
            content_hash, order_hash = fingerprint(body, response.get("Content-Type", ""))
            error = None
        except Exception as exc:  # a view that blows up is itself a finding
            status = None
            size = 0
            content_hash = order_hash = None
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - start) * 1000.0

    queries = ctx.captured_queries
    shapes = Counter(normalize_sql(q["sql"]) for q in queries)
    duplicates = sum(count - 1 for count in shapes.values() if count > 1)
    worst_shape, worst_count = (shapes.most_common(1) or [("", 0)])[0]

    return {
        "status": status,
        "error": error,
        "response_bytes": size,
        "content_hash": content_hash,
        "order_hash": order_hash,
        "wall_ms": round(elapsed_ms, 2),
        "query_count": len(queries),
        "db_ms": round(sum(float(q.get("time", 0) or 0) for q in queries) * 1000.0, 2),
        "duplicate_queries": duplicates,
        "distinct_shapes": len(shapes),
        "worst_repeat_count": worst_count,
        "worst_repeat_sql": worst_shape[:400],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "workload.yml"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--reps", type=int, default=2, help="measured reps after warmup; last one is recorded")
    ap.add_argument("--only", help="substring filter on scenario id")
    ap.add_argument("--tag", help="only scenarios carrying this tag")
    ap.add_argument("--dump-urls", help="also write the resolved URL list here, for Tier 2")
    args = ap.parse_args()

    endpoints, problems = workload_mod.resolve(args.workload)
    if problems:
        print(f"!! {len(problems)} scenario(s) failed to resolve:", file=sys.stderr)
        for pr in problems:
            print(f"   {pr['id']}: {pr['reason']}", file=sys.stderr)
    if args.only:
        endpoints = [e for e in endpoints if args.only in e["id"]]
    if args.tag:
        endpoints = [e for e in endpoints if args.tag in e["tags"]]
    if args.dump_urls:
        with open(args.dump_urls, "w") as fh:
            json.dump(endpoints, fh, indent=2)
    client = get_perf_client()
    print(f"profiling {len(endpoints)} endpoints", file=sys.stderr)

    records = []
    for i, sc in enumerate(endpoints, 1):
        sid, url = sc["id"], sc["url"]
        measure(client, url)  # warmup: prime content-type / permission caches

        # Run the same request several times and check whether its content hash
        # holds still. Rather than guessing which endpoints embed volatile data,
        # we measure it: an endpoint whose hash moves across identical requests
        # cannot be content-gated, and is marked for manual review instead.
        reps = [measure(client, url) for _ in range(max(2, args.reps))]
        result = reps[-1]
        hashes = {r["content_hash"] for r in reps}
        qcounts = {r["query_count"] for r in reps}
        result["content_stable"] = len(hashes) == 1
        result["query_count_stable"] = len(qcounts) == 1
        if len(qcounts) > 1:
            result["query_count_range"] = [min(qcounts), max(qcounts)]
        result.update({"id": sid, "url": url, "tags": sc["tags"]})
        records.append(result)
        flag = ""
        if result["status"] not in (200, 302):
            flag = f"  <-- status {result['status']}"
        if result["duplicate_queries"] > 20:
            flag += f"  <-- {result['duplicate_queries']} dup queries"
        print(f"[{i}/{len(endpoints)}] {sid} q={result['query_count']}{flag}", file=sys.stderr)

    records.sort(key=lambda r: r["query_count"], reverse=True)
    payload = {
        "schema": 2,
        "endpoint_count": len(records),
        "unresolved": problems,
        "endpoints": records,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
