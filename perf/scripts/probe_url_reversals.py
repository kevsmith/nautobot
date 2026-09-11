#!/usr/bin/env python
"""Count URL reversals per request, and how many of them are redundant.

`reverse()` costs ~51us: namespace resolution against a URLconf Nautobot mounts twice,
a reverse_dict lookup, substitution, then a regex re-match to prove the URL routes back.
Django caches every structure involved -- get_resolver, get_ns_resolver, reverse_dict --
and never caches the answer, because the answer depends on the thread-local script prefix
and urlconf. Only the caller can know those are stable.

So a template that reverses a constant inside a loop pays per iteration. This counts them,
groups by (viewname, args, kwargs), and reports how many calls were repeats of a result
already computed in the same request. That count is a deterministic counter: it does not
move with machine load, which is what makes it usable as an A/B gate where wall clock at
this magnitude is not.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_url_reversals.py
"""

import argparse
import collections
import functools
import json
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

import django.urls  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

SCENARIOS = [
    ("ui.chrome.404", "/dcim/no-such-page-chrome-control/", {}),
    ("ui.home", "/", {}),
    ("ui.device.list", "/dcim/devices/", {}),
    ("ui.device.list.rows", "/dcim/devices/", {"HTTP_HX_REQUEST": "true"}),
]


class ReversalSpy:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        self._orig = django.urls.reverse
        spy = self

        @functools.wraps(self._orig)
        def wrapper(viewname, *a, **kw):
            t = time.perf_counter()
            try:
                return spy._orig(viewname, *a, **kw)
            finally:
                spy.calls.append((
                    str(viewname),
                    tuple(kw.get("args") or ()),
                    tuple(sorted((kw.get("kwargs") or {}).items())),
                    (time.perf_counter() - t) * 1e6,
                ))
        # `from django.urls import reverse` binds the function object into the importing module at
        # import time, so patching `django.urls.reverse` alone is invisible to every module that did
        # that -- including `rest_framework.reverse`, which is the whole of the REST API's reversing.
        # An earlier version of this probe reported ZERO reversals for `/api/dcim/devices/?limit=100`,
        # which is not a fast endpoint, it is a blind instrument. Rebind every alias instead.
        self._aliases = []
        for module in list(sys.modules.values()):
            if module is None:
                continue
            try:
                members = list(vars(module).items())
            except TypeError:  # pragma: no cover - some module objects have no __dict__
                continue
            for attr, value in members:
                if value is self._orig:
                    self._aliases.append((module, attr))
                    setattr(module, attr, wrapper)
        return self

    def __exit__(self, *exc):
        for module, attr in self._aliases:
            setattr(module, attr, self._orig)

    def summary(self):
        grouped = collections.Counter((n, a, k) for n, a, k, _ in self.calls)
        return {
            "reversals": len(self.calls),
            "distinct": len(grouped),
            "redundant": sum(c - 1 for c in grouped.values()),
            "reverse_us": sum(d for *_, d in self.calls),
            "top": [{"name": n, "calls": c} for (n, _, _), c in grouped.most_common(3)],
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "urls",
        nargs="*",
        help="URLs to profile instead of the built-in UI scenarios; each is named after itself.",
    )
    args = ap.parse_args()

    scenarios = [(url, url, {}) for url in args.urls] or SCENARIOS
    client = get_perf_client()
    out = []
    for name, url, extra in scenarios:
        client.get(url, **extra)  # warm-up, discarded
        runs = []
        for _ in range(args.reps):
            with ReversalSpy() as spy:
                t = time.perf_counter()
                resp = client.get(url, **extra)
                wall = (time.perf_counter() - t) * 1000.0
            s = spy.summary()
            s["wall_ms"] = wall
            s["status"] = resp.status_code
            runs.append(s)
        med = {
            "id": name,
            "status": runs[0]["status"],
            "reversals": int(statistics.median([r["reversals"] for r in runs])),
            "distinct": int(statistics.median([r["distinct"] for r in runs])),
            "redundant": int(statistics.median([r["redundant"] for r in runs])),
            "reverse_ms": round(statistics.median([r["reverse_us"] for r in runs]) / 1000, 1),
            "wall_ms": round(statistics.median([r["wall_ms"] for r in runs]), 1),
            "top": runs[-1]["top"],
        }
        out.append(med)
        if not args.json:
            top = ", ".join(f"{t['name']}x{t['calls']}" for t in med["top"])
            print(f"  {name:46s} status={med['status']} reversals={med['reversals']:>4} "
                  f"distinct={med['distinct']:>3} redundant={med['redundant']:>4} "
                  f"reverse={med['reverse_ms']:>6.1f}ms wall={med['wall_ms']:>7.1f}ms  {top}")
    if args.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
