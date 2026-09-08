#!/usr/bin/env python
"""Price the {% url %} inside inc/nav_favorites.html's loop, at a realistic favourite count.

Finding 51 fixed the same pattern in inc/nav_menu.html -- two argument-free URL names
reversed once per menu item, 369 reversals per page -- and deliberately left this one out,
because the measurement user has no favourites and the loop body never executes. Changing
it without measuring it would have put an unmeasured change in a measured commit.

`inc/nav_favorites.html:21` reverses `user:navbar_favorites_delete` inside
`{% for item in request.user.navbar_favorites %}`, so the cost is one reversal per
favourite at ~51us each. Line 4's reorder URL is outside the loop and costs one per page.

This sweeps the favourite count so the prize is a function rather than a guess, and so the
stopping rule in perf/queue.md can be applied to it honestly: the fragment-caching candidate
there was parked for measuring ~0.6ms, and this may land in the same territory.

Favourites live in the user's config blob (`user.set_config("navbar_favorites", ...)`), not
a table, so they are cheap to plant and are removed again on the way out.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_nav_favorites.py
"""

import argparse
import collections
import functools
import json
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

import django.urls  # noqa: E402
from django.template.base import Template as BaseTemplate  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

URL = "/dcim/no-such-page-chrome-control/"   # chrome only: no list machinery in the way


def plant(user, n):
    """Give the user n favourites, shaped as the template expects."""
    favs = [
        {"link": f"/dcim/devices/?page={i}", "name": f"Favourite {i}",
         "tab_name": "Inventory", "group_name": "Devices"}
        for i in range(n)
    ]
    user.set_config("navbar_favorites", favs, commit=True)
    return len(favs)


class Spy:
    """Count reversals, and time inc/nav_favorites.html specifically."""

    def __init__(self):
        self.calls = collections.Counter()
        self.reverse_us = 0.0
        self.fav_ms = 0.0
        self.fav_renders = 0

    def __enter__(self):
        self._rev = django.urls.reverse
        self._tpl = BaseTemplate.render
        spy = self

        @functools.wraps(self._rev)
        def reverse(viewname, *a, **kw):
            t = time.perf_counter()
            try:
                return spy._rev(viewname, *a, **kw)
            finally:
                spy.reverse_us += (time.perf_counter() - t) * 1e6
                spy.calls[str(viewname)] += 1

        def render(tpl_self, *a, **kw):
            name = getattr(tpl_self, "name", None) or getattr(
                getattr(tpl_self, "origin", None), "template_name", None) or ""
            if name != "inc/nav_favorites.html":
                return spy._tpl(tpl_self, *a, **kw)
            spy.fav_renders += 1
            t = time.perf_counter()
            try:
                return spy._tpl(tpl_self, *a, **kw)
            finally:
                spy.fav_ms += (time.perf_counter() - t) * 1000.0

        django.urls.reverse = reverse
        BaseTemplate.render = render
        return self

    def __exit__(self, *exc):
        django.urls.reverse = self._rev
        BaseTemplate.render = self._tpl


def measure(client, reps):
    runs = []
    client.get(URL)  # warm-up, discarded
    for _ in range(reps):
        with Spy() as s:
            t = time.perf_counter()
            resp = client.get(URL)
            wall = (time.perf_counter() - t) * 1000.0
        runs.append({
            "wall": wall, "status": resp.status_code, "bytes": len(resp.content),
            "reversals": sum(s.calls.values()),
            "delete_calls": s.calls.get("user:navbar_favorites_delete", 0),
            "reverse_ms": s.reverse_us / 1000.0,
            "fav_ms": s.fav_ms, "fav_renders": s.fav_renders,
        })
    return {k: statistics.median([r[k] for r in runs]) for k in runs[0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", default="0,5,10,20,50")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from django.contrib.auth import get_user_model

    client = get_perf_client()
    user = get_user_model().objects.get(username="perfbot")
    original = user.get_config("navbar_favorites", [])
    out = []
    try:
        for n in [int(c) for c in args.counts.split(",") if c != ""]:
            planted = plant(user, n)
            m = measure(client, args.reps)
            m["favourites"] = planted
            out.append(m)
            if not args.json:
                print(f"  favourites={planted:>3} status={int(m['status'])} "
                      f"reversals={int(m['reversals']):>4} delete_url_calls={int(m['delete_calls']):>3} "
                      f"reverse={m['reverse_ms']:>6.2f}ms nav_favorites.html={m['fav_ms']:>6.2f}ms "
                      f"(x{int(m['fav_renders'])}) wall={m['wall']:>7.1f}ms bytes={int(m['bytes']):>7}")
    finally:
        user.set_config("navbar_favorites", original, commit=True)
        if not args.json:
            print(f"  restored the user's original {len(original)} favourite(s)")
    if args.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
