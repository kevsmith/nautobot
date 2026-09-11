#!/usr/bin/env python
"""What do tags cost a read, in queries and bytes?

Run on one arm against both snapshots. The question is whether tags add *queries* or only
*payload*: a serializer touches `obj.tags` per row whether or not any tags come back, so a
zero-tag dataset may already pay the query and differ only in what it returns.
"""
import json
import os
import time

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection, reset_queries  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from nautobot.extras.models import TaggedItem  # noqa: E402
from nautobot.users.models import Token  # noqa: E402

token = Token.objects.filter(key="0123456789abcdef0123456789abcdef01234567").first()
client = APIClient()
client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

URLS = [
    "/api/dcim/interfaces/?limit=25",
    "/api/dcim/interfaces/?limit=100",
    "/api/dcim/interfaces/?limit=100&depth=1",
    "/api/dcim/devices/?limit=100",
    "/api/extras/object-changes/?limit=25",
]
print(f"tagged_items in this database: {TaggedItem.objects.count():,}")
out = {}
for url in URLS:
    client.get(url, SERVER_NAME="localhost")          # warm caches
    with CaptureQueriesContext(connection) as ctx:
        t0 = time.perf_counter()
        r = client.get(url, SERVER_NAME="localhost")
        ms = (time.perf_counter() - t0) * 1000
    body = r.content
    tagq = sum(1 for q in ctx.captured_queries if "taggeditem" in q["sql"].lower() or "extras_tag" in q["sql"].lower())
    out[url] = {"queries": len(ctx.captured_queries), "tag_queries": tagq, "bytes": len(body), "ms": round(ms, 1)}
    print(f"  {url:44} {len(ctx.captured_queries):5d} q ({tagq} tag) {len(body):9,} B  {ms:7.1f} ms")
print("JSON " + json.dumps(out))
