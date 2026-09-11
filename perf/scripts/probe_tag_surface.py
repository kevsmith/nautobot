#!/usr/bin/env python
"""How much tag surface does each instrument actually render?

A dataset can carry tags and an instrument still never see one: the read screen requests
`?limit=25` and the first 25 interfaces in default ordering carry none, while `?limit=100`
renders tags on about half. This counts, per endpoint, how many *rendered* rows carry a tag --
which is the only number that says whether a measurement on this dataset can show anything.
"""
import json
import os

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.urls import reverse, NoReverseMatch  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from nautobot.extras.models import TaggedItem  # noqa: E402
from nautobot.users.models import Token  # noqa: E402

token = Token.objects.filter(key="0123456789abcdef0123456789abcdef01234567").first()
client = APIClient()
client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

# Which models carry tags at all, and how densely.
print("=== tagged models in this dataset ===")
dense = {}
for ct_id, in TaggedItem.objects.values_list("content_type_id").distinct():
    ct = ContentType.objects.get(pk=ct_id)
    M = ct.model_class()
    total = M.objects.count()
    tagged = TaggedItem.objects.filter(content_type_id=ct_id).values("object_id").distinct().count()
    assigns = TaggedItem.objects.filter(content_type_id=ct_id).count()
    dense[f"{ct.app_label}.{ct.model}"] = (total, tagged, assigns)
    print(f"  {ct.app_label}.{ct.model:16} {tagged:6,} of {total:6,} tagged ({tagged/total*100:4.1f}%), {assigns:,} assignments")

# What each instrument's request shape actually renders.
print("\n=== rendered tag surface, by request shape ===")
SHAPES = [
    ("dcim-api:interface-list", [("limit", 25), ("limit", 50), ("limit", 100), ("limit", 100, 1)]),
    ("dcim-api:device-list", [("limit", 25), ("limit", 50), ("limit", 100), ("limit", 100, 1)]),
]
for route, variants in SHAPES:
    try:
        base = reverse(route)
    except NoReverseMatch:
        print(f"  {route}: no reverse"); continue
    for v in variants:
        depth = v[2] if len(v) > 2 else 0
        url = f"{base}?limit={v[1]}" + (f"&depth={depth}" if depth else "")
        r = client.get(url, SERVER_NAME="localhost")
        if r.status_code != 200:
            print(f"  {url:56} HTTP {r.status_code}"); continue
        rows = r.json().get("results", [])
        n = sum(1 for x in rows if x.get("tags"))
        total_tags = sum(len(x.get("tags") or []) for x in rows)
        print(f"  {url:56} {n:4d}/{len(rows):4d} rows tagged, {total_tags:5d} tag objects rendered")

# The tag endpoint itself.
print("\n=== the tag endpoints ===")
for route in ("extras-api:tag-list",):
    r = client.get(reverse(route) + "?limit=25", SERVER_NAME="localhost")
    print(f"  {route}: HTTP {r.status_code}, {r.json().get('count')} rows")
