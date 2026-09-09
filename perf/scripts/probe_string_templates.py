#!/usr/bin/env python
"""Where do `<string>` templates come from, and is each render recompiling one?

probe_page_phases.py reports a template named `<string>` -- Django's origin name for a
Template built from a string rather than loaded from a file -- as one of the more expensive
entries on several pages:

    ui.home              61.2ms exclusive over    6 renders   (10.2ms each)
    ui.device.list.rows 118.3ms exclusive over  500 renders   (0.24ms each)
    ui.rack.detail        2.1ms exclusive over    8 renders

10.2ms for one small template render is roughly 40x what the file-backed templates in the
same run cost, which is the signature of compilation happening per render instead of once.
Compiling is `Template.__init__` -> `compile_nodelist()` -> Lexer/Parser over the source;
rendering a compiled nodelist is a different and much cheaper operation.

So this counts and times both halves separately, and attributes each construction to the
Nautobot line that asked for it:

  constructions   Template.__init__ calls whose origin is a string, with the source's first
                  60 characters and the nearest Nautobot frame
  renders         Template.render calls on string-origin templates
  ratio           constructions / renders. ~1.0 means every render recompiles and the fix is
                  to cache the compiled Template. ~0 means compilation is already amortised
                  and the cost is genuinely in rendering, which would send this back to
                  page_phases for a different explanation.

Counts are deterministic; the millisecond figures are not, so read them on a quiet box.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_string_templates.py \
        [scenario-id ...]
"""

import collections
import os
import sys
import time
import traceback

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.template.base import Template  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402
import workload as workload_mod  # noqa: E402

DEFAULT_SCENARIOS = ["ui.home", "ui.device.list.rows", "ui.device.list", "ui.device.detail"]


def nearest_nautobot_frame():
    """The nearest frame in Nautobot's own code, which is where a fix would go."""
    for frame in reversed(traceback.extract_stack()):
        path = frame.filename
        if "/nautobot/" in path and "/django/" not in path and "site-packages" not in path:
            return f"{path.split('/nautobot/')[-1]}:{frame.lineno} {frame.name}"
    return "(no nautobot frame)"


class StringTemplateTracker:
    """Count and time construction and rendering of string-origin Templates."""

    def __init__(self):
        self.constructions = collections.Counter()
        self.construct_ms = collections.Counter()
        self.sources = {}
        self.render_n = 0
        self.render_ms = 0.0
        self.construct_n = 0
        self._orig_init = Template.__init__
        self._orig_render = Template.render

    def _is_string_origin(self, template):
        origin = getattr(template, "origin", None)
        name = getattr(origin, "name", None) or getattr(template, "name", None)
        return name in (None, "<unknown source>", "<string>")

    def __enter__(self):
        tracker = self

        def init(template, template_string, origin=None, name=None, engine=None):
            start = time.perf_counter()
            tracker._orig_init(template, template_string, origin, name, engine)
            elapsed = (time.perf_counter() - start) * 1000.0
            if tracker._is_string_origin(template):
                site = nearest_nautobot_frame()
                tracker.constructions[site] += 1
                tracker.construct_ms[site] += elapsed
                tracker.construct_n += 1
                snippet = " ".join(str(template_string).split())[:60]
                tracker.sources.setdefault(site, snippet)
            return None

        def render(template, context):
            if not tracker._is_string_origin(template):
                return tracker._orig_render(template, context)
            start = time.perf_counter()
            try:
                return tracker._orig_render(template, context)
            finally:
                tracker.render_ms += (time.perf_counter() - start) * 1000.0
                tracker.render_n += 1

        Template.__init__ = init
        Template.render = render
        return self

    def __exit__(self, *exc):
        Template.__init__ = self._orig_init
        Template.render = self._orig_render
        return False


def main():
    ids = sys.argv[1:] or DEFAULT_SCENARIOS
    resolved, _ = workload_mod.resolve(workload_mod.DEFAULT_WORKLOAD)
    rows = {r["id"]: r for r in resolved}
    missing = [i for i in ids if i not in rows]
    if missing:
        sys.exit(f"unknown scenario(s): {', '.join(missing)}")

    client = get_perf_client()
    for sid in ids:
        row = rows[sid]
        url, headers = row["url"], row.get("headers") or {}
        client.get(url, headers=headers)  # warm; a cold process compiles everything once

        with StringTemplateTracker() as t:
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"{sid}: NON-200 {resp.status_code}, skipped")
            continue

        ratio = (t.construct_n / t.render_n) if t.render_n else float("nan")
        print(f"\n=== {sid} ===")
        print(f"  string-template renders      {t.render_n:6d}   {t.render_ms:8.1f} ms")
        print(f"  string-template compilations {t.construct_n:6d}   {sum(t.construct_ms.values()):8.1f} ms")
        print(f"  compilations per render      {ratio:6.2f}   "
              f"({'recompiled per render' if ratio > 0.5 else 'compilation amortised'})")
        if t.constructions:
            print("  --- compiled at ---")
            for site, n in t.constructions.most_common(8):
                print(f"    {n:5d}x {t.construct_ms[site]:7.1f}ms  {site}")
                print(f"           source: {t.sources.get(site, '')!r}")


if __name__ == "__main__":
    main()
