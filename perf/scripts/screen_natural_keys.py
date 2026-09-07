#!/usr/bin/env python
"""Which models pay for a traversing natural key, and which targets are worth denormalizing?

`BaseModel.natural_key()` resolves each entry in `natural_key_field_lookups` with a
plain `getattr` walk, so every lookup containing `__` is a lazy foreign-key load --
one query per hop, per object, every time the key is computed. Serialization computes
it constantly: change logging, `natural_slug`, and every nested representation.

Finding 14 fixed the worst case by loading `{pk: natural key values}` for the whole
target table once, but only two models opt in (Location, Tenant) and the map lives in
a `request_cache()` scope, so it returns None for Jobs, data migrations and nbshell.
A fragment stored on the *target* record would do the same work durably and without a
scope.

This ranks where that would pay. For every model it reports the hops its natural key
walks, resolves what each hop traverses *into*, and weights by how many rows exist --
because a five-hop key on a five-row table costs nothing and a two-hop key on 8,925
interfaces costs plenty.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/screen_natural_keys.py
"""

import argparse
from collections import Counter, defaultdict
import json
import os

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.apps import apps  # noqa: E402
from django.core.exceptions import FieldDoesNotExist  # noqa: E402

from nautobot.core.models.utils import cache_natural_key_field_lookups  # noqa: E402


def hop_targets(model, lookup):
    """Every model traversed by one `a__b__c` lookup, in order."""
    targets, current = [], model
    for part in lookup.split("__")[:-1]:
        try:
            field = current._meta.get_field(part)
        except (FieldDoesNotExist, AttributeError):
            break
        related = getattr(field, "related_model", None)
        if related is None:
            break
        targets.append(related)
        current = related
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    rows = []
    skipped = []
    # How many source rows would traverse *into* each target model, and from where.
    into_rows = Counter()
    into_sources = defaultdict(set)
    enabled = {}

    with cache_natural_key_field_lookups():
        for model in apps.get_models():
            lookups = getattr(model, "natural_key_field_lookups", None)
            if not lookups:
                continue
            try:
                lookups = list(lookups)
                count = model.objects.count()
            except Exception as exc:
                skipped.append((model._meta.label_lower, f"{type(exc).__name__}: {exc}"[:80]))
                continue
            traversing = [lookup for lookup in lookups if "__" in lookup]
            hops = sum(lookup.count("__") for lookup in lookups)
            label = model._meta.label_lower
            enabled[label] = bool(getattr(model, "natural_key_map_enabled", False))
            rows.append(
                {
                    "model": label,
                    "rows": count,
                    "lookups": len(lookups),
                    "traversing_lookups": len(traversing),
                    "hops": hops,
                    # Hops actually walked when computing one object's key.
                    "queries_per_object": hops,
                    "predicted_queries": hops * count,
                }
            )
            for lookup in traversing:
                for target in hop_targets(model, lookup):
                    into_rows[target._meta.label_lower] += count
                    into_sources[target._meta.label_lower].add(label)

    rows.sort(key=lambda r: -r["predicted_queries"])
    print(f"{'model':46s} {'rows':>7} {'hops':>5} {'q/obj':>6} {'predicted':>12}")
    for r in rows[: args.top]:
        if not r["hops"]:
            continue
        print(
            f"{r['model']:46s} {r['rows']:>7} {r['hops']:>5} {r['queries_per_object']:>6} {r['predicted_queries']:>12,}"
        )

    print(f"\n{'traversed INTO':46s} {'src models':>11} {'src rows':>10}  map?")
    for target, src_rows in into_rows.most_common(args.top):
        flag = "yes" if enabled.get(target) else "--"
        print(f"{target:46s} {len(into_sources[target]):>11} {src_rows:>10,}  {flag}")

    if skipped:
        print(
            f"\n{len(skipped)} model(s) could not be introspected: "
            + ", ".join(f"{m} ({why})" for m, why in skipped[:4])
        )
    total = sum(r["predicted_queries"] for r in rows)
    walking = sum(1 for r in rows if r["hops"])
    print(
        f"\n{walking} of {len(rows)} models have a traversing natural key; "
        f"{total:,} hop-queries to serialize every object once"
    )

    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(
                {
                    "schema": 1,
                    "models": rows,
                    "traversed_into": {
                        k: {
                            "source_models": sorted(into_sources[k]),
                            "source_rows": v,
                            "map_enabled": enabled.get(k, False),
                        }
                        for k, v in into_rows.items()
                    },
                },
                fh,
                indent=2,
                sort_keys=True,
                default=str,
            )
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
