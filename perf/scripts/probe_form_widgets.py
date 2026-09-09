#!/usr/bin/env python
"""Attribute a list view's form-widget rendering to widget classes and option counts.

The filter drawer is 16.6-53.2ms of every list view's document request, and roughly
55-60% of that is per-option and per-attribute template rendering rather than anything
specific to the drawer:

    ui.prefix.list   drawer 53.2ms   selectwithdisabled_option.html x138 = 19.5ms
                                     django/forms/widgets/attrs.html x207 = 10.5ms

Django's `select.html` renders `{% include option.template_name with widget=option %}`
once per option, and Nautobot's `selectwithdisabled_option.html` then includes
`attrs.html` itself, so each option costs two template renders. The templates are
already cached (`cached.Loader`, verified: the inner Template and nodelist are identical
across `get_template` calls), so this is genuine render overhead -- context push/pop and
node traversal -- not re-parsing.

A Python fast path can only replace the *base* option template. Four subclasses override
`option_template_name` (`ColorSelect`, `SelectWithPK`, `ContentTypeSelect`, and
`SelectWithDisabled` itself), so anything that overrides it must fall back. This reports
which widget classes actually render the options, so the fast path is written for the
classes that carry the cost rather than for all of them.

Per scenario: each widget class's instance count, total options rendered, and the time
inside its `render()`. Counts are deterministic; the ms figures are not.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_form_widgets.py \
        [scenario-id ...]
"""

import collections
import os
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.forms.widgets import ChoiceWidget, Widget  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402
import workload as workload_mod  # noqa: E402

DEFAULT_SCENARIOS = ["ui.prefix.list", "ui.ipaddress.list", "ui.device.list", "ui.circuit.list"]


class WidgetTracker:
    """Time Widget.render per class, and count the options ChoiceWidget emits."""

    def __init__(self):
        self.renders = collections.Counter()
        self.ms = collections.Counter()
        self.options = collections.Counter()
        self.option_template = collections.defaultdict(set)
        self._orig_render = Widget.render
        self._orig_optgroups = ChoiceWidget.optgroups

    def __enter__(self):
        tracker = self

        def render(widget, name, value, attrs=None, renderer=None):
            cls = type(widget).__name__
            start = time.perf_counter()
            try:
                return tracker._orig_render(widget, name, value, attrs, renderer)
            finally:
                tracker.ms[cls] += (time.perf_counter() - start) * 1000.0
                tracker.renders[cls] += 1

        def optgroups(widget, name, value, attrs=None):
            groups = tracker._orig_optgroups(widget, name, value, attrs)
            cls = type(widget).__name__
            tracker.options[cls] += sum(len(choices) for _, choices, _ in groups)
            tracker.option_template[cls].add(getattr(widget, "option_template_name", None))
            return groups

        Widget.render = render
        ChoiceWidget.optgroups = optgroups
        return self

    def __exit__(self, *exc):
        Widget.render = self._orig_render
        ChoiceWidget.optgroups = self._orig_optgroups
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
        client.get(url, headers=headers)  # warm

        with WidgetTracker() as t:
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"{sid}: NON-200 {resp.status_code}, skipped")
            continue

        print(f"\n=== {sid} ===")
        print(f"  {'widget class':34s} {'renders':>8} {'options':>8} {'ms':>8}  option_template")
        total_opts = 0
        for cls, n in t.renders.most_common(14):
            opts = t.options.get(cls, 0)
            total_opts += opts
            tmpl = ", ".join(sorted(str(x).replace("widgets/", "") for x in t.option_template[cls])) or "-"
            print(f"  {cls:34s} {n:8d} {opts:8d} {t.ms[cls]:7.1f}  {tmpl}")
        print(f"  {'TOTAL':34s} {sum(t.renders.values()):8d} {sum(t.options.values()):8d} "
              f"{sum(t.ms.values()):7.1f}")


if __name__ == "__main__":
    main()
