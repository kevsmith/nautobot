#!/usr/bin/env python
"""Which cache-backend reads still scale with the number of rows in a response?

Ken Celenza's `API | Caching` series (PR 1 of his breakout manifest) is six commits
aimed at one shape: a value read once per serialized object, where each read is a
Django-cache -- that is, Redis -- round trip. His last commit reports its detector at
"zero per-object cache GETs at depth 0 and 1" afterwards.

This is that detector for our tree. Findings 4, 5 and 6 attacked the same shape by a
different route: `cache_get_or_set()` and `get_settings_or_config()` memoize into the
`request_cache()` scope, and `cache_natural_key_field_lookups()` memoizes the natural-key
recipe for a read request. If those cover it, a key's GET count does not move when the
page grows from 25 rows to 100. Any key that *does* move is a site his series would
still remove and ours does not.

Counts backend `.get`/`.get_many` calls, attributed by key prefix, at two page sizes.
Deterministic: cache reads do not vary with machine load.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_cache_gets.py
"""

import argparse
import collections
import json
import os
import re
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.core.cache import caches as _django_caches  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Each scenario is a URL template carrying {n}, so the same request can be issued at two
# page sizes. Chosen to cover every site the imported series touches: Location natural
# keys and display (depth 0 and 1), Platform's NETWORK_DRIVERS read, JobQueue's
# get_celery_queues, custom-field keys on a model that has them, and one UI list -- the UI
# is outside the request-scoped natural-key cache that finding 5 installed.
SCENARIOS = [
    ("api.location.list", "/api/dcim/locations/?limit={n}", {}),
    ("api.location.depth1", "/api/dcim/locations/?limit={n}&depth=1", {}),
    ("api.device.list", "/api/dcim/devices/?limit={n}", {}),
    ("api.device.depth1", "/api/dcim/devices/?limit={n}&depth=1", {}),
    ("api.interface.list", "/api/dcim/interfaces/?limit={n}", {}),
    ("api.platform.list", "/api/dcim/platforms/?limit={n}", {}),
    ("api.jobqueue.list", "/api/extras/job-queues/?limit={n}", {}),
    ("ui.location.list.rows", "/dcim/locations/?per_page={n}", {"HTTP_HX_REQUEST": "true"}),
    ("ui.device.list.rows", "/dcim/devices/?per_page={n}", {"HTTP_HX_REQUEST": "true"}),
    ("api.rackgroup.depth1", "/api/dcim/rack-groups/?limit={n}&depth=1", {}),
    ("api.rack.depth1", "/api/dcim/racks/?limit={n}&depth=1", {}),
    ("api.tenant.depth1", "/api/tenancy/tenants/?limit={n}&depth=1", {}),
    ("api.prefix.depth1", "/api/ipam/prefixes/?limit={n}&depth=1", {}),
    ("api.interface.depth1", "/api/dcim/interfaces/?limit={n}&depth=1", {}),
    ("api.locationtype.list", "/api/dcim/location-types/?limit={n}", {}),
    ("ui.location.detail.rows", "/dcim/locations/?per_page={n}&depth=1", {"HTTP_HX_REQUEST": "true"}),
]


def normalize(key):
    """Collapse the varying part of a key so per-instance keys group together."""
    return UUID_RE.sub("<uuid>", str(key))


class CacheGetCounter:
    """Count reads that reach the cache backend, attributed by normalized key."""

    def __init__(self):
        self.keys = collections.Counter()

    def __enter__(self):
        self._cls = type(_django_caches["default"])
        self._get, self._get_many = self._cls.get, self._cls.get_many
        counter = self

        def counting_get(cache_self, key, *a, **kw):
            counter.keys[normalize(key)] += 1
            return counter._get(cache_self, key, *a, **kw)

        def counting_get_many(cache_self, keys, *a, **kw):
            for key in keys:
                counter.keys[normalize(key)] += 1
            return counter._get_many(cache_self, keys, *a, **kw)

        self._cls.get, self._cls.get_many = counting_get, counting_get_many
        return self

    def __exit__(self, *exc):
        self._cls.get, self._cls.get_many = self._get, self._get_many


def measure(client, url, extra):
    client.get(url, **extra)  # warm-up, discarded: process caches are cold once only
    with CacheGetCounter() as counter:
        resp = client.get(url, **extra)
    return resp, counter.keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--small", type=int, default=25)
    ap.add_argument("--large", type=int, default=100)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_perf_client()
    out = []
    for name, template, extra in SCENARIOS:
        small_resp, small = measure(client, template.format(n=args.small), extra)
        large_resp, large = measure(client, template.format(n=args.large), extra)
        rows = args.large - args.small
        # A key whose count rises with the page is read per object; one that does not is
        # already memoized for the request, whatever the mechanism.
        scaling = {
            key: {
                "small": small[key],
                "large": large[key],
                "per_row": round((large[key] - small[key]) / rows, 3),
            }
            for key in set(small) | set(large)
            if large[key] > small[key]
        }
        rec = {
            "id": name,
            "status": [small_resp.status_code, large_resp.status_code],
            "gets_small": sum(small.values()),
            "gets_large": sum(large.values()),
            "scaling_keys": dict(sorted(scaling.items(), key=lambda kv: -kv[1]["per_row"])),
            "top_keys": [{"key": k, "n": c} for k, c in large.most_common(5)],
        }
        out.append(rec)
        if not args.json:
            print(f"  {name:24s} status={rec['status']} gets {rec['gets_small']:>4} -> {rec['gets_large']:>4}")
            if scaling:
                for key, v in rec["scaling_keys"].items():
                    print(f"      PER-ROW {v['per_row']:>6.3f}  {v['small']:>4} -> {v['large']:>4}  {key}")
            else:
                print("      no key scales with row count")
    if args.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
