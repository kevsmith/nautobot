#!/usr/bin/env python3
"""Tier 2: per-endpoint wall-clock latency via cassowary.

cassowary reports metrics only in aggregate -- neither its JSON summary nor its
raw CSV carries a per-URL column -- so pointing it at a whole URL file yields
one blended number. This driver therefore invokes it *once per endpoint* and
merges the results, which also keeps a single slow view from skewing the rest.

Runs on the host against the published port (default 8180).

    python3 perf/tier2_latency.py --endpoints perf/endpoints.yml \
        --token "$NAUTOBOT_TOKEN" --session "$SESSIONID" \
        --out perf/results/tier2-baseline.json
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


def cassowary_once(url, headers, requests, concurrency, timeout):
    """Run one cassowary load test, returning its parsed JSON metrics."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        metrics_path = tf.name
    cmd = [
        "cassowary", "run",
        "-u", url,
        "-n", str(requests),
        "-c", str(concurrency),
        "-t", str(timeout),
        "-s",
        "-F", "--json-metrics-file", metrics_path,
    ]
    for h in headers:
        cmd += ["-H", h]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        with open(metrics_path) as fh:
            data = json.load(fh)
        data["_cassowary_rc"] = proc.returncode
        return data
    except json.JSONDecodeError:
        return {"_error": "no metrics written", "_stderr": (proc.stdout or proc.stderr or "").strip()[:200]}
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            os.unlink(metrics_path)
        except OSError:
            pass



class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a 3xx into an HTTPError instead of silently chasing it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(url, headers, timeout):
    """One plain request, to record what the endpoint actually returns.

    cassowary reports no status code -- neither its JSON summary nor this
    driver's output carried one -- and it counts any answered request as a
    success. A Tier 2 run against UI endpoints with only an API token therefore
    reported 27 endpoints, zero failures, and timed a 299KB HTTP 403 page in
    ~50ms apiece, which looked like a 6-16x improvement over the previous
    baseline. Nothing in the result file could have revealed that.

    So every endpoint is probed once before it is load-tested, and its status
    travels with its timing. An endpoint that does not answer 200/302 is
    recorded and skipped rather than timed.
    """
    req = urllib.request.Request(url)
    for h in headers:
        name, _, value = h.partition(": ")
        req.add_header(name, value)
    # Do NOT follow redirects. urlopen follows them by default, and an
    # unauthenticated UI request 302s to /login/?next=... -- so the first
    # version of this probe chased the redirect, got 200 back with a 14,888-byte
    # login page, and cleared the endpoint for timing. Only 200 counts here: a
    # redirect means the thing that would be timed is not the page under test.
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, len(resp.read()), None
    except urllib.error.HTTPError as exc:
        return exc.code, len(exc.read() or b""), exc.headers.get("Location")
    except Exception as exc:  # noqa: BLE001 -- reported, not raised
        return None, str(exc), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", required=True,
                    help="resolved URL JSON, written by tier1_queries.py --dump-urls")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", default="http://localhost:8180")
    ap.add_argument("--token", default=os.environ.get("NAUTOBOT_TOKEN", ""))
    ap.add_argument("--session", default=os.environ.get("NAUTOBOT_SESSIONID", ""))
    ap.add_argument("-n", "--requests", type=int, default=30)
    ap.add_argument("-c", "--concurrency", type=int, default=4)
    ap.add_argument("-t", "--timeout", type=int, default=60)
    ap.add_argument("--only", help="substring filter on view name")
    ap.add_argument("--urls-from-tier1", help="tier1 JSON; restrict to its top-N slowest endpoints")
    ap.add_argument("--top", type=int, default=0, help="with --urls-from-tier1, how many to take")
    args = ap.parse_args()

    with open(args.urls) as fh:
        scenarios = json.load(fh)
    pairs = [(sc["id"], sc["url"]) for sc in scenarios
             if sc.get("method", "GET") == "GET"
             and (not args.only or args.only in sc["id"])]

    # Optionally narrow to what Tier 1 already flagged as expensive -- Tier 2 is
    # the slow, noisy confirmation step, so it is worth spending only where it counts.
    if args.urls_from_tier1 and args.top:
        with open(args.urls_from_tier1) as fh:
            t1 = json.load(fh)
        ranked = [(r["id"], r["url"]) for r in t1["endpoints"] if r["status"] == 200]
        wanted = set(ranked[: args.top])
        pairs = [p for p in pairs if p in wanted]

    print(f"load-testing {len(pairs)} endpoints "
          f"(n={args.requests} c={args.concurrency})", file=sys.stderr)

    records = []
    for i, (view_name, url) in enumerate(pairs, 1):
        full = args.base_url.rstrip("/") + url
        headers = []
        # cassowary splits header values on commas, so a multi-value Accept is
        # rejected outright. Send one value -- and send it: without an explicit
        # Accept, DRF serves the *browsable HTML API* for /api/ endpoints, which
        # is far slower than JSON and would make every API number meaningless.
        if url.startswith("/api/"):
            if args.token:
                headers.append(f"Authorization: Token {args.token}")
            headers.append("Accept: application/json")
        elif args.session:
            headers.append(f"Cookie: sessionid={args.session}")

        status, size, location = probe(full, headers, args.timeout)
        if status != 200:
            where = f" -> {location}" if location else ""
            print(f"[{i}/{len(pairs)}] {view_name} SKIPPED -- probe returned "
                  f"{status}{where} ({size} bytes); not timing an endpoint that "
                  f"is not serving the page under test", file=sys.stderr)
            records.append({
                "id": view_name, "url": url, "probe_status": status,
                "probe_bytes": size if isinstance(size, int) else None,
                "skipped": f"probe status {status}"
                           + (f" -> {location}" if location else ""),
                "requests": None, "failed": None, "rps": None,
                "server_ms_mean": None, "server_ms_median": None,
                "server_ms_p95": None, "error": None if isinstance(size, int) else size,
            })
            continue

        m = cassowary_once(full, headers, args.requests, args.concurrency, args.timeout)
        if m.get("_error") or m.get("_stderr"):
            print(f"    cassowary: {m.get('_stderr') or m.get('_error')}", file=sys.stderr)
        sp = m.get("server_processing", {}) or {}
        rec = {
            "id": view_name,
            "url": url,
            "requests": m.get("total_requests"),
            "failed": m.get("failed_requests"),
            "rps": m.get("requests_per_second"),
            "server_ms_mean": sp.get("mean"),
            "server_ms_median": sp.get("median"),
            "server_ms_p95": sp.get("95th_percentile"),
            "error": m.get("_error"),
            "probe_status": status,
            "probe_bytes": size,
            "skipped": None,
        }
        records.append(rec)
        print(f"[{i}/{len(pairs)}] {view_name} "
              f"p95={rec['server_ms_p95']}ms failed={rec['failed']}", file=sys.stderr)

    records.sort(key=lambda r: (r.get("server_ms_p95") or 0), reverse=True)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"schema": 1, "base_url": args.base_url,
                   "requests": args.requests, "concurrency": args.concurrency,
                   "endpoints": records}, fh, indent=2, sort_keys=True)
    skipped = [r for r in records if r.get("skipped")]
    if skipped:
        print(f"\n!! {len(skipped)} of {len(records)} endpoints were NOT timed:",
              file=sys.stderr)
        for r in skipped:
            print(f"     {r['id']}: probe {r['probe_status']}", file=sys.stderr)
        print("   UI views need session auth (--session / NAUTOBOT_SESSIONID); an API\n"
              "   token authenticates DRF only and yields 403 on UI endpoints.",
              file=sys.stderr)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
