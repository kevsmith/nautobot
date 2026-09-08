#!/usr/bin/env python
"""Is probe_page_phases.py measuring the page, or measuring itself?

Two questions, because this branch has been wrong about one of these numbers before:
cProfile put nav_menu at 40ms/request when the true figure was ~16ms, and the rule that
came out of it is that profiler output is shape, never magnitude. probe_page_phases.py
reports inc/nav_menu.html at ~48ms exclusive, which is close enough to the discredited
figure to need proving.

  1. INSTRUMENT OVERHEAD. Same scenario, patches off then on, same process. If the
     instrumented wall clock is materially higher, every per-template figure is inflated
     by some unknown share and none of them can be quoted.

  2. IS nav_menu REALLY ~48ms? Independent of the timing wrapper: stub the template so it
     renders to the empty string and measure how much wall clock disappears. If ~48ms
     goes, the attribution is right. If far less goes, the wrapper is over-attributing
     and the 48ms belongs somewhere else.

Arms alternate and rep 1 of each is discarded.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_phase_validate.py
"""

import argparse
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.template.base import Template as BaseTemplate  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_page_phases import Phases  # noqa: E402
from tier1_queries import get_perf_client  # noqa: E402

URL = "/dcim/devices/"


def plain(client, reps):
    out = []
    for _ in range(reps + 1):
        t = time.perf_counter()
        client.get(URL)
        out.append((time.perf_counter() - t) * 1000.0)
    return out[1:]


def instrumented(client, reps):
    out = []
    for _ in range(reps + 1):
        p = Phases()
        p.install()
        try:
            t = time.perf_counter()
            client.get(URL)
            out.append((time.perf_counter() - t) * 1000.0)
        finally:
            p.remove()
    return out[1:]


def stubbed(client, reps, target):
    """Render `target` to the empty string; time the page without it."""
    orig = BaseTemplate.render

    def render(tpl_self, *a, **kw):
        name = getattr(tpl_self, "name", None) or getattr(
            getattr(tpl_self, "origin", None), "template_name", None) or ""
        if name == target:
            return ""
        return orig(tpl_self, *a, **kw)

    BaseTemplate.render = render
    try:
        out = []
        for _ in range(reps + 1):
            t = time.perf_counter()
            client.get(URL)
            out.append((time.perf_counter() - t) * 1000.0)
        return out[1:]
    finally:
        BaseTemplate.render = orig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=7)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--target", default="inc/nav_menu.html")
    args = ap.parse_args()
    client = get_perf_client()

    acc = {"plain": [], "instrumented": [], "stubbed": []}
    for rnd in range(args.rounds):
        order = [("plain", plain), ("instrumented", instrumented), ("stubbed", stubbed)]
        if rnd % 2:
            order.reverse()
        for name, fn in order:
            vals = fn(client, args.reps) if name != "stubbed" else fn(client, args.reps, args.target)
            acc[name].append(statistics.median(vals))
            print(f"  round {rnd + 1} {name:13s} median {statistics.median(vals):7.1f}ms  "
                  f"(min {min(vals):.1f} max {max(vals):.1f})")

    print()
    p = statistics.median(acc["plain"])
    i = statistics.median(acc["instrumented"])
    s = statistics.median(acc["stubbed"])
    print(f"  plain          {p:7.1f}ms   uninstrumented baseline")
    print(f"  instrumented   {i:7.1f}ms   overhead {i - p:+.1f}ms ({(i - p) / p * 100:+.1f}%)")
    print(f"  {args.target} stubbed {s:7.1f}ms   removes {p - s:.1f}ms ({(p - s) / p * 100:.1f}% of the page)")
    print(f"  probe_page_phases attributed ~48ms exclusive to {args.target}")


if __name__ == "__main__":
    main()
