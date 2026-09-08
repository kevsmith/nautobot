#!/usr/bin/env python
"""Attribute one template's render time across the node types inside it.

Finding 51 removed ~28ms of inc/nav_menu.html's ~47ms by hoisting 369 redundant URL
reversals. What remains is ~20ms across 15 tabs, 42 groups and 123 items, and the open
question is whether that is a hot spot worth chasing or ~2,000 template nodes at ~10us
each -- which would be ordinary Django interpretation and not fixable by anything short of
rendering the menu less often.

probe_page_phases.py attributes per *template*. This goes one level down, wrapping
`django.template.base.Node.render` and bucketing by node class, but only while the target
template is on the stack, so the rest of the page is not instrumented. Exclusive time is
tracked the same way -- nodes nest, so inclusive time double-counts.

**This probe's overhead is not negligible and is measured, not assumed.** Wrapping every
node render on a template with thousands of nodes costs real time, so `--validate` runs the
same request plain and instrumented and reports the difference. Read that before believing
any per-node figure: probe_page_phases came in at +1.6% and was quotable, and this one will
not be as cheap.

Also reports the compiled node inventory, which needs no timing at all and gives the
denominator: if 2,000 nodes render in 20ms then the per-node cost is the answer and there is
nothing to optimise but the count.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_template_nodes.py
    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_template_nodes.py --validate
"""

import argparse
import collections
import json
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.template.base import Node, Template as BaseTemplate  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

DEFAULT_TEMPLATE = "inc/nav_menu.html"
DEFAULT_URL = "/dcim/no-such-page-chrome-control/"   # chrome only, nothing else in the way


def _template_name(tpl):
    return getattr(tpl, "name", None) or getattr(
        getattr(tpl, "origin", None), "template_name", None) or "<string>"


class NodeSpy:
    """Time every Node.render that happens while `target` is rendering."""

    def __init__(self, target):
        self.target = target
        self.inside = 0
        self.stack = []
        self.by_type = collections.defaultdict(lambda: {"n": 0, "incl": 0.0, "excl": 0.0})
        self.template_ms = 0.0

    def __enter__(self):
        self._tpl = BaseTemplate.render
        self._node = Node.render
        spy = self

        def tpl_render(tpl_self, *a, **kw):
            if _template_name(tpl_self) != spy.target:
                return spy._tpl(tpl_self, *a, **kw)
            spy.inside += 1
            t = time.perf_counter()
            try:
                return spy._tpl(tpl_self, *a, **kw)
            finally:
                spy.template_ms += (time.perf_counter() - t) * 1000.0
                spy.inside -= 1

        def node_render(node_self, context):
            if not spy.inside:
                return spy._node(node_self, context)
            frame = {"child": 0.0}
            spy.stack.append(frame)
            t = time.perf_counter()
            try:
                return spy._node(node_self, context)
            finally:
                incl = (time.perf_counter() - t) * 1000.0
                spy.stack.pop()
                rec = spy.by_type[type(node_self).__name__]
                rec["n"] += 1
                rec["incl"] += incl
                rec["excl"] += incl - frame["child"]
                if spy.stack:
                    spy.stack[-1]["child"] += incl

        BaseTemplate.render = tpl_render
        Node.render = node_render
        return self

    def __exit__(self, *exc):
        BaseTemplate.render = self._tpl
        Node.render = self._node


def inventory(target):
    """Count the compiled template's nodes by type -- no timing, no overhead."""
    from django.template.loader import get_template

    tpl = get_template(target).template
    counts = collections.Counter()

    def walk(nodelist):
        for node in nodelist:
            counts[type(node).__name__] += 1
            for attr in getattr(node, "child_nodelists", ()):
                child = getattr(node, attr, None)
                if child:
                    walk(child)

    walk(tpl.nodelist)
    return counts


def validate(client, url, target, reps):
    """How much does this probe cost? Plain against instrumented, alternating."""
    plain, instr = [], []
    client.get(url)
    for i in range(reps):
        if i % 2:
            t = time.perf_counter(); client.get(url); plain.append((time.perf_counter() - t) * 1000.0)
            with NodeSpy(target):
                t = time.perf_counter(); client.get(url); instr.append((time.perf_counter() - t) * 1000.0)
        else:
            with NodeSpy(target):
                t = time.perf_counter(); client.get(url); instr.append((time.perf_counter() - t) * 1000.0)
            t = time.perf_counter(); client.get(url); plain.append((time.perf_counter() - t) * 1000.0)
    p, i = statistics.median(plain), statistics.median(instr)
    return {"plain_ms": round(p, 1), "instrumented_ms": round(i, 1),
            "overhead_ms": round(i - p, 1), "overhead_pct": round((i - p) / p * 100, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--validate", action="store_true", help="measure this probe's own overhead first")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_perf_client()
    out = {"template": args.template, "url": args.url}

    inv = inventory(args.template)
    out["compiled_nodes"] = {"total": sum(inv.values()), "by_type": dict(inv.most_common())}

    if args.validate:
        out["overhead"] = validate(client, args.url, args.template, args.reps)

    runs = []
    client.get(args.url)  # warm-up, discarded
    for _ in range(args.reps):
        with NodeSpy(args.template) as spy:
            t = time.perf_counter()
            client.get(args.url)
            wall = (time.perf_counter() - t) * 1000.0
        runs.append((wall, spy.template_ms, {k: dict(v) for k, v in spy.by_type.items()}))

    out["wall_ms"] = round(statistics.median([r[0] for r in runs]), 1)
    out["template_ms"] = round(statistics.median([r[1] for r in runs]), 1)
    types = sorted({k for _, _, d in runs for k in d})
    rows = []
    for t in types:
        recs = [d[t] for _, _, d in runs if t in d]
        rows.append({"node": t,
                     "renders": int(statistics.median([r["n"] for r in recs])),
                     "excl_ms": round(statistics.median([r["excl"] for r in recs]), 2),
                     "incl_ms": round(statistics.median([r["incl"] for r in recs]), 2)})
    rows.sort(key=lambda r: -r["excl_ms"])
    out["nodes"] = rows

    if args.json:
        print(json.dumps(out, indent=2))
        return

    print(f"  template {args.template}   compiled nodes: {out['compiled_nodes']['total']}")
    if "overhead" in out:
        o = out["overhead"]
        print(f"  probe overhead: plain {o['plain_ms']}ms -> instrumented {o['instrumented_ms']}ms "
              f"({o['overhead_ms']:+}ms, {o['overhead_pct']:+}%)")
    print(f"  wall {out['wall_ms']}ms   {args.template} render {out['template_ms']}ms")
    total_excl = sum(r["excl_ms"] for r in rows)
    print(f"  {'node type':28s} {'renders':>8} {'excl_ms':>9} {'incl_ms':>9} {'us/render':>10}")
    for r in rows[:args.top]:
        per = r["excl_ms"] / r["renders"] * 1000 if r["renders"] else 0
        print(f"  {r['node']:28s} {r['renders']:>8} {r['excl_ms']:>9.2f} {r['incl_ms']:>9.2f} {per:>10.1f}")
    print(f"  {'TOTAL exclusive':28s} {sum(r['renders'] for r in rows):>8} {total_excl:>9.2f}")


if __name__ == "__main__":
    main()
