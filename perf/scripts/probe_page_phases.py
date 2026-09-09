#!/usr/bin/env python
"""Split a page's wall clock into view work, template rendering, and database wait.

`ui.device.list` is 283ms wall, 275.8ms of it user-mode CPU, 4.5ms database, 18 Redis
reads worth under a millisecond, and it renders a table over `queryset.none()`
(finding 44). Against `ui.chrome.404` at 64ms for full chrome, ~218ms is per-model list
machinery over zero rows. This says where.

Three boundaries, measured rather than subtracted:

  render phase   SimpleTemplateResponse.render -- the view has already returned by then,
                 so wall minus this is view work: filtersets, tables, permissions.
  per template   django.template.base.Template.render, with a stack so each template
                 gets inclusive AND exclusive time. An include costs its parent
                 inclusive time; only exclusive time says where the work is.
  database       bucketed by whether the query fired inside a render, because templates
                 evaluate lazy querysets and a naive subtraction would double-count.

`cpu_ms` is reported alongside so the phases can be checked against something that is
not derived from them. cProfile is deliberately absent: this branch put nav_menu at 40ms
profiled against ~16ms true, so profiler output is for shape in a separate run, never
for magnitude here.

One warm-up rep is discarded; process caches are cold on the first pass.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_page_phases.py
"""

import argparse
import collections
import json
import os
import resource
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.template.base import Template as BaseTemplate  # noqa: E402
from django.template.response import SimpleTemplateResponse  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402
import workload as workload_mod  # noqa: E402

SCENARIOS = [
    ("ui.chrome.404", "/dcim/no-such-page-chrome-control/", {}),
    ("ui.home", "/", {}),
    ("ui.device.detail", "/dcim/devices/001d7a3d-4b7b-4d8f-9bc7-494bc1669e7a/", {}),
    ("ui.rack.detail", "/dcim/racks/19c90458-45f4-46ea-96fa-3a8cf224f3d5/", {}),
    ("ui.device.list", "/dcim/devices/", {}),
    ("ui.device.list.rows", "/dcim/devices/", {"HTTP_HX_REQUEST": "true"}),
    ("api.device.list", "/api/dcim/devices/?limit=50", {}),
]


class Phases:
    """Time the render phase, each template, and where queries land."""

    def __init__(self):
        self.render_ms = 0.0
        self.in_render = 0
        self.db_in_render = 0.0
        self.db_outside = 0.0
        self.q_in_render = 0
        self.q_outside = 0
        self.stack = []
        self.templates = collections.defaultdict(lambda: {"incl": 0.0, "excl": 0.0, "n": 0})

    # --- db wrapper --------------------------------------------------------
    def __call__(self, execute, sql, params, many, context):
        t = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            d = (time.perf_counter() - t) * 1000.0
            if self.in_render:
                self.db_in_render += d
                self.q_in_render += 1
            else:
                self.db_outside += d
                self.q_outside += 1

    # --- patches -----------------------------------------------------------
    def install(self):
        self._resp_render = SimpleTemplateResponse.render
        self._tpl_render = BaseTemplate.render
        probe = self

        def resp_render(resp_self, *a, **kw):
            t = time.perf_counter()
            probe.in_render += 1
            try:
                return probe._resp_render(resp_self, *a, **kw)
            finally:
                probe.in_render -= 1
                probe.render_ms += (time.perf_counter() - t) * 1000.0

        def tpl_render(tpl_self, *a, **kw):
            name = getattr(tpl_self, "name", None) or getattr(
                getattr(tpl_self, "origin", None), "template_name", None) or "<string>"
            frame = {"child": 0.0}
            probe.stack.append(frame)
            t = time.perf_counter()
            try:
                return probe._tpl_render(tpl_self, *a, **kw)
            finally:
                incl = (time.perf_counter() - t) * 1000.0
                probe.stack.pop()
                rec = probe.templates[name]
                rec["incl"] += incl
                rec["excl"] += incl - frame["child"]
                rec["n"] += 1
                if probe.stack:
                    probe.stack[-1]["child"] += incl

        SimpleTemplateResponse.render = resp_render
        BaseTemplate.render = tpl_render

    def remove(self):
        SimpleTemplateResponse.render = self._resp_render
        BaseTemplate.render = self._tpl_render


def one(client, url, extra):
    p = Phases()
    p.install()
    try:
        r0 = resource.getrusage(resource.RUSAGE_SELF)
        with connection.execute_wrapper(p):
            t0 = time.perf_counter()
            resp = client.get(url, **extra)
            wall = (time.perf_counter() - t0) * 1000.0
        r1 = resource.getrusage(resource.RUSAGE_SELF)
    finally:
        p.remove()
    cpu = ((r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)) * 1000.0
    return {
        "status": resp.status_code,
        "wall_ms": round(wall, 1),
        "cpu_ms": round(cpu, 1),
        "render_ms": round(p.render_ms, 1),
        "view_ms": round(wall - p.render_ms, 1),
        "db_in_render_ms": round(p.db_in_render, 1),
        "db_outside_ms": round(p.db_outside, 1),
        "q_in_render": p.q_in_render,
        "q_outside": p.q_outside,
        "templates": {k: {kk: round(vv, 1) if isinstance(vv, float) else vv for kk, vv in v.items()}
                      for k, v in p.templates.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "scenarios",
        nargs="*",
        help="workload scenario ids to measure instead of the built-in SCENARIOS list. "
        "The built-in list is a fixed set chosen to contrast page shapes; a question about "
        "one template across many pages needs a different set, and hardcoding meant editing "
        "the probe to ask it.",
    )
    args = ap.parse_args()

    if args.scenarios:
        resolved, _ = workload_mod.resolve(workload_mod.DEFAULT_WORKLOAD)
        rows = {r["id"]: r for r in resolved}
        missing = [i for i in args.scenarios if i not in rows]
        if missing:
            sys.exit(f"unknown scenario(s): {', '.join(missing)}")
        scenarios = [
            (
                sid,
                rows[sid]["url"],
                {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in (rows[sid].get("headers") or {}).items()},
            )
            for sid in args.scenarios
        ]
    else:
        scenarios = SCENARIOS

    client = get_perf_client()
    out = []
    for name, url, extra in scenarios:
        runs = [one(client, url, extra) for _ in range(args.reps + 1)][1:]
        med = {k: statistics.median([r[k] for r in runs]) for k in runs[0] if k != "templates"}
        agg = collections.defaultdict(lambda: {"incl": [], "excl": [], "n": []})
        for r in runs:
            for tname, v in r["templates"].items():
                for kk in ("incl", "excl", "n"):
                    agg[tname][kk].append(v[kk])
        med["id"] = name
        med["templates"] = sorted(
            ({"name": t, "excl_ms": round(statistics.median(v["excl"]), 1),
              "incl_ms": round(statistics.median(v["incl"]), 1),
              "renders": int(statistics.median(v["n"]))} for t, v in agg.items()),
            key=lambda d: -d["excl_ms"])
        out.append(med)
        if not args.json:
            print(f"  {name}")
            print(f"    wall={med['wall_ms']:.1f}  cpu={med['cpu_ms']:.1f}  "
                  f"view={med['view_ms']:.1f}  render={med['render_ms']:.1f}  "
                  f"db_in_render={med['db_in_render_ms']:.1f} ({int(med['q_in_render'])}q)  "
                  f"db_outside={med['db_outside_ms']:.1f} ({int(med['q_outside'])}q)")
            for t in med["templates"][:args.top]:
                print(f"      {t['excl_ms']:>7.1f}ms excl  {t['incl_ms']:>7.1f}ms incl  "
                      f"x{t['renders']:<4} {t['name']}")
    if args.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
