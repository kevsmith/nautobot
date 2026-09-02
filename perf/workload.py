"""Resolve perf/workload.yml into concrete, runnable URLs.

Kept separate from the harnesses so Tier 1 (in-container, Django) and Tier 2
(on the host, no Django) can agree on exactly one definition of the workload.
Tier 1 resolves it live; Tier 2 consumes the JSON that Tier 1 dumps.
"""

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
        sid, view = sc["id"], sc["view"]
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
        resolved.append({"id": sid, "url": url, "tags": sc.get("tags", []),
                         "method": sc.get("method", "GET")})
    return resolved, problems
