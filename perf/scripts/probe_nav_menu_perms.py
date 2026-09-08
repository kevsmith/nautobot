#!/usr/bin/env python
"""Is the nav menu's cost a superuser-only measurement?

Every figure in findings 50 and 51 was taken as `perfbot`, a superuser. That matters
because `_build_nav_menu` runs ~176 permission checks through `has_one_or_more_perms`,
which loops `user.has_perm(...)` and returns on the first True -- and Nautobot's
ObjectPermissionBackend answers True immediately for a superuser. So the measured
"construction is 1.1ms, rendering is 48.4ms, 44x more" may hold only for a user who
short-circuits every check on its first iteration.

For a user with real permissions each failing check walks the whole list, and the backend
has to load ObjectPermissions at least once. This measures both users through the same
path: build time, permission-check counts, queries issued during the build, the size of
the menu that results, and the template render.

A non-superuser also sees fewer menu items, so a cheaper render would be a coverage
artifact rather than a saving -- hence item counts are reported alongside the timings.

    perf/scripts/dc.sh exec -T nautobot python /source/perf/scripts/probe_nav_menu_perms.py
"""

import argparse
import json
import os
import statistics
import sys
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.auth.models import Permission  # noqa: E402
from django.db import connection  # noqa: E402
from django.template.base import Template as BaseTemplate  # noqa: E402
from django.test.client import Client  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier1_queries import get_perf_client  # noqa: E402

LIMITED_USER = "perfbot_limited"
BROAD_USER = "perfbot_broad"
# A read-only operator with a handful of permissions: sees almost no menu, so most of the
# permission tree is pruned before it is checked.
LIMITED_PERMS = [
    ("dcim", "view_device"), ("dcim", "view_rack"), ("dcim", "view_location"),
    ("ipam", "view_prefix"), ("ipam", "view_ipaddress"), ("extras", "view_status"),
]


def make_client(username, perms):
    """A non-superuser with exactly `perms`, or every view_* permission when None."""
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username=username, defaults={"is_superuser": False, "is_staff": False, "is_active": True})
    user.is_superuser = False
    user.is_staff = False
    user.is_active = True
    user.save()
    user.user_permissions.clear()
    if perms is None:
        # The expensive case: sees essentially the whole menu, and every check goes through
        # the backend because nothing short-circuits on is_superuser.
        #
        # It must be granted through ObjectPermission, not django.contrib.auth Permission.
        # ObjectPermissionBackend.get_object_permissions() reads the ObjectPermission model
        # and nothing else, so `user.user_permissions` is inert here -- a user granted 192
        # Django view permissions still saw a 1-tab menu and a 403.
        from django.contrib.contenttypes.models import ContentType

        from nautobot.users.models import ObjectPermission

        op, _ = ObjectPermission.objects.get_or_create(
            name="perf-probe-view-all", defaults={"actions": ["view"], "enabled": True})
        op.actions = ["view"]
        op.enabled = True
        op.save()
        op.object_types.set(ContentType.objects.all())
        op.users.set([user])
        granted = op.object_types.count()
    else:
        granted = 0
        for app_label, codename in perms:
            pm = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
            if pm:
                user.user_permissions.add(pm)
                granted += 1
    user = User.objects.get(pk=user.pk)
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client, user, granted


def _unused_limited_client():
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username=LIMITED_USER, defaults={"is_superuser": False, "is_staff": False, "is_active": True})
    user.is_superuser = False
    user.is_staff = False
    user.is_active = True
    user.save()
    user.user_permissions.clear()
    for app_label, codename in LIMITED_PERMS:
        p = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
        if p:
            user.user_permissions.add(p)
    user = User.objects.get(pk=user.pk)  # drop any cached permission state
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client, user


class Counters:
    """Count permission checks, time the build, and time the menu template."""

    def __init__(self):
        self.build_ms = 0.0
        self.has_one_calls = 0
        self.has_perm_calls = 0
        self.nav_render_ms = 0.0
        self.build_queries = 0
        self.obj_perm_loads = 0
        self._in_build = 0

    def __call__(self, execute, sql, params, many, context):
        if self._in_build:
            self.build_queries += 1
        return execute(sql, params, many, context)

    def install(self):
        from nautobot.core import authentication as auth
        from nautobot.core import context_processors as cp

        self._build = cp._build_nav_menu
        self._has_one = cp.has_one_or_more_perms
        self._tpl = BaseTemplate.render
        self._backend_has_perm = auth.ObjectPermissionBackend.has_perm
        self._get_obj_perms = auth.ObjectPermissionBackend.get_object_permissions
        c = self

        def backend_has_perm(be_self, user_obj, perm, obj=None):
            c.has_perm_calls += 1
            return c._backend_has_perm(be_self, user_obj, perm, obj)

        def get_obj_perms(be_self, user_obj):
            c.obj_perm_loads += 1
            return c._get_obj_perms(be_self, user_obj)

        auth.ObjectPermissionBackend.has_perm = backend_has_perm
        auth.ObjectPermissionBackend.get_object_permissions = get_obj_perms

        def build(request):
            c._in_build += 1
            t = time.perf_counter()
            try:
                return c._build(request)
            finally:
                c.build_ms += (time.perf_counter() - t) * 1000.0
                c._in_build -= 1

        def has_one(user, perms):
            # Count the wrapper; individual checks are counted at the backend, below.
            # Patching type(user).has_perm does not work inside a request: the user is a
            # SimpleLazyObject proxy with no has_perm on the class.
            c.has_one_calls += 1
            return c._has_one(user, perms)

        def tpl(tpl_self, *a, **kw):
            name = getattr(tpl_self, "name", None) or getattr(
                getattr(tpl_self, "origin", None), "template_name", None) or ""
            if name != "inc/nav_menu.html":
                return c._tpl(tpl_self, *a, **kw)
            t = time.perf_counter()
            try:
                return c._tpl(tpl_self, *a, **kw)
            finally:
                c.nav_render_ms += (time.perf_counter() - t) * 1000.0

        cp._build_nav_menu = build
        cp.has_one_or_more_perms = has_one
        BaseTemplate.render = tpl

    def remove(self):
        from nautobot.core import authentication as auth
        from nautobot.core import context_processors as cp
        cp._build_nav_menu = self._build
        cp.has_one_or_more_perms = self._has_one
        BaseTemplate.render = self._tpl
        auth.ObjectPermissionBackend.has_perm = self._backend_has_perm
        auth.ObjectPermissionBackend.get_object_permissions = self._get_obj_perms


def menu_size(user):
    from django.test import RequestFactory
    from nautobot.core import context_processors as cp
    req = RequestFactory().get("/dcim/devices/")
    req.user = user
    ctx = cp._build_nav_menu(req)
    tabs = (ctx.get("nav_menu") or {}).get("tabs", {})
    groups = sum(len(t.get("groups", {})) for t in tabs.values())
    items = sum(len(g.get("items", {})) for t in tabs.values() for g in t.get("groups", {}).values())
    return len(tabs), groups, items


def run(label, client, user, url, reps):
    runs = []
    client.get(url)  # warm-up, discarded
    for _ in range(reps):
        c = Counters()
        c.install()
        try:
            with connection.execute_wrapper(c):
                t = time.perf_counter()
                resp = client.get(url)
                wall = (time.perf_counter() - t) * 1000.0
        finally:
            c.remove()
        runs.append({"wall": wall, "build_ms": c.build_ms, "nav_ms": c.nav_render_ms,
                     "has_one": c.has_one_calls, "has_perm": c.has_perm_calls,
                     "build_q": c.build_queries, "obj_perm_loads": c.obj_perm_loads, "status": resp.status_code,
                     "bytes": len(resp.content)})
    med = {k: statistics.median([r[k] for r in runs]) for k in runs[0]}
    med.update({"label": label, "menu": menu_size(user)})
    return med


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="/dcim/devices/")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    su_client = get_perf_client()
    su = get_user_model().objects.get(username="perfbot")
    lim_client, lim, lim_n = make_client(LIMITED_USER, LIMITED_PERMS)
    broad_client, broad, broad_n = make_client(BROAD_USER, None)

    out = [run("superuser", su_client, su, args.url, args.reps),
           run(f"limited ({lim_n} perms)", lim_client, lim, args.url, args.reps),
           run(f"broad ({broad_n} view perms)", broad_client, broad, args.url, args.reps)]
    if args.json:
        print(json.dumps(out, indent=2))
        return
    for m in out:
        t, g, i = m["menu"]
        print(f"  {m['label']:24s} status={int(m['status'])} wall={m['wall']:>7.1f}ms "
              f"build={m['build_ms']:>6.2f}ms nav_render={m['nav_ms']:>6.1f}ms "
              f"has_one_or_more_perms={int(m['has_one']):>4} has_perm={int(m['has_perm']):>5} "
              f"build_q={int(m['build_q']):>3} objperm_loads={int(m['obj_perm_loads'])} menu={t}/{g}/{i} bytes={int(m['bytes']):>7}")


if __name__ == "__main__":
    main()
