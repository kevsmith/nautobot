"""Resolve perf/workload.yml into concrete, runnable URLs.

Kept separate from the harnesses so Tier 1 (in-container, Django) and Tier 2
(on the host, no Django) can agree on exactly one definition of the workload.
Tier 1 resolves it live; Tier 2 consumes the JSON that Tier 1 dumps.
"""

import os as _os

# perf/workload.yml -- one directory up from perf/scripts/. Three callers each built
# this path from their own __file__, so `ae517a228` moving the harness into
# perf/scripts/ broke Tier 1, attribute.py and bench_endpoints.py in one commit. The
# committed baselines still verified, so nothing complained until Tier 1 was next run
# through run_experiment.sh, which does not pass --workload. Defined once here so a
# fourth caller cannot diverge again.
DEFAULT_WORKLOAD = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "workload.yml")


from urllib.parse import urlencode

from django.apps import apps
from django.db.models import Count
from django.urls import NoReverseMatch, reverse
import yaml


def _pick_object(spec):
    """Select one object deterministically. Never a hardcoded PK."""
    model = apps.get_model(spec["model"])
    strategy = spec.get("strategy", "first")
    qs = model.objects.all()
    if strategy == "first":
        return qs.order_by("pk").first()
    if strategy == "last":
        return qs.order_by("pk").last()
    if strategy.startswith("max_related:"):
        related = strategy.split(":", 1)[1]
        # Tie-break on pk so the choice is stable across identical datasets.
        return qs.annotate(_perf_n=Count(related)).order_by("-_perf_n", "pk").first()
    raise ValueError(f"unknown pick strategy: {strategy}")


def resolve(path):
    """Return (resolved, problems).

    ``resolved`` is a list of dicts with id/url/tags. ``problems`` lists
    scenarios that could not be resolved -- a renamed view or an object type
    absent from the dataset -- so the workload fails loudly rather than
    silently shrinking.
    """
    with open(path) as fh:
        doc = yaml.safe_load(fh)

    resolved, problems = [], []
    for sc in doc.get("scenarios", []):
        if sc.get("skip"):
            continue
        sid = sc["id"]
        # A literal path, used by exactly one kind of scenario: a control that
        # is supposed to 404. The rule everywhere else is that a URL comes from
        # reverse() so a renamed view fails loudly instead of silently
        # measuring a redirect -- but a 404 has no view to reverse, which is the
        # whole point of it. Declaring `path` opts out of the rule explicitly
        # rather than by accident.
        if sc.get("path"):
            resolved.append({"id": sid, "url": sc["path"], "tags": sc.get("tags", []),
                             "method": sc.get("method", "GET"),
                             "headers": sc.get("headers") or {},
                             "expected_status": sc.get("expected_status", 200)})
            continue
        view = sc["view"]
        try:
            if "pick" in sc:
                obj = _pick_object(sc["pick"])
                if obj is None:
                    problems.append({"id": sid, "reason": f"no {sc['pick']['model']} objects in dataset"})
                    continue
                url = reverse(view, args=[obj.pk])
            else:
                url = reverse(view)
        except NoReverseMatch as exc:
            problems.append({"id": sid, "reason": f"view name did not reverse: {exc}"})
            continue
        except LookupError as exc:
            problems.append({"id": sid, "reason": f"model lookup failed: {exc}"})
            continue

        if sc.get("query"):
            url = f"{url}?{urlencode(sc['query'])}"
        # `headers` carries the request as the browser actually sends it. A
        # Nautobot list view renders its table over queryset.none() unless
        # HX-Request is present (core/views/renderers.py), so a scenario without
        # that header measures a page with no rows in it.
        # `expected_status` exists for control scenarios that are supposed to
        # fail. A 404 renders the full chrome -- 40x.html extends base.html --
        # with a static card for content, which makes it the cheapest available
        # measurement of what every page pays before it renders anything of its
        # own. Without this the harness flags it and Tier 2 refuses to time it.
        resolved.append({"id": sid, "url": url, "tags": sc.get("tags", []),
                         "method": sc.get("method", "GET"),
                         "headers": sc.get("headers") or {},
                         "expected_status": sc.get("expected_status", 200)})
    return resolved, problems
