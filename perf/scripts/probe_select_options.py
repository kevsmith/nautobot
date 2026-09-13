#!/usr/bin/env python
"""Separate a select widget's fixed cost from its per-option cost.

`probe_form_widgets.py` attributes 25.4ms to one `StaticSelect2Multiple` rendering 129
options, and 26.1ms to two `StaticSelect2` rendering 133 between them. Those are whole-widget
figures: they cannot say how much is the `<select>` wrapper and how much is the options, and
a fix that precomputes the options only pays off on the second part.

Vary the option count and fit. The intercept is the wrapper and everything else that does not
scale with options; the slope is the per-option cost. Fitting is what makes the split measured
rather than assumed -- subtracting one whole-widget figure from another would fold the
wrapper's own variation into the answer.

Reported per widget class, because `StaticSelect2Multiple` renders through `SelectMultiple`
and may not share a slope with `StaticSelect2`.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_select_options.py
"""

import argparse
import os
import statistics
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from nautobot.core.forms.utils import add_blank_choice  # noqa: E402
from nautobot.core.forms.widgets import StaticSelect2, StaticSelect2Multiple  # noqa: E402

# The two real fields this exists to price, so the fitted line can be checked against a
# measurement taken on the actual page rather than only against itself.
REAL = {"prefix_length": 130, "mask_length": 129}


def time_render(widget, name, value, reps):
    """Median ms for one `render()`, after a discarded warm-up."""
    widget.render(name, value, attrs={"id": f"id_{name}"})
    runs = []
    for _ in range(reps):
        start = time.perf_counter()
        widget.render(name, value, attrs={"id": f"id_{name}"})
        runs.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(runs)


def fit(points):
    """Least-squares slope and intercept over (option_count, ms)."""
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def run(cls, label, counts, reps, multiple):
    print(f"\n=== {label} ===")
    print(f"  {'options':>8} {'ms':>9} {'ms/option':>11}")
    points = []
    for k in counts:
        widget = cls()
        widget.choices = add_blank_choice([(i, i) for i in range(k)])
        total = k + 1  # add_blank_choice prepends one
        # Select something real, so the branch that marks an option selected is exercised
        # rather than skipped -- that branch runs once per option either way.
        value = [str(k - 1)] if multiple else str(k - 1)
        ms = time_render(widget, "probe", value, reps)
        points.append((total, ms))
        print(f"  {total:>8} {ms:>9.3f} {ms / total * 1000:>10.1f}us")
    slope, intercept = fit(points)
    print(f"  fit: {intercept:.3f}ms fixed + {slope * 1000:.1f}us per option")
    for name, count in REAL.items():
        print(f"       at {name}'s {count} options -> {intercept + slope * count:.1f}ms "
              f"({slope * count / (intercept + slope * count) * 100:.0f}% options)")
    return points, slope, intercept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=25)
    ap.add_argument("--counts", default="0,1,2,5,10,25,50,100,129")
    args = ap.parse_args()
    counts = [int(c) for c in args.counts.split(",")]

    run(StaticSelect2, "StaticSelect2 (prefix_length, ip_version)", counts, args.reps, multiple=False)
    run(StaticSelect2Multiple, "StaticSelect2Multiple (mask_length)", counts, args.reps, multiple=True)


if __name__ == "__main__":
    main()
