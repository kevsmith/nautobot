#!/usr/bin/env python
"""Do tagged objects cost tag queries when they are change-logged?

Finding 31 fixed `serialize_object`'s `_tags` cache. Finding 22 then stopped writing
`object_data`, which removed the `serialize_object` call from `to_objectchange` -- leaving
`serialize_object_v2`, which does not read `_tags`. This counts, on one arm, how many
`extras_taggeditem` queries a batch update of tagged objects issues, and whether
`tag_cache_warmed`'s batch query is still one of them.
"""
import collections
import json
import os

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from nautobot.dcim.models import Interface  # noqa: E402
from nautobot.extras.models import TaggedItem  # noqa: E402
from nautobot.users.models import Token  # noqa: E402

N = int(os.environ.get("PROBE_N", "50"))
token = Token.objects.filter(key="0123456789abcdef0123456789abcdef01234567").first()
client = APIClient()
client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

tagged_ids = list(
    TaggedItem.objects.filter(content_type__model="interface").values_list("object_id", flat=True).distinct()[:N]
)
untagged_ids = list(
    Interface.objects.exclude(pk__in=TaggedItem.objects.filter(content_type__model="interface").values("object_id"))
    .values_list("pk", flat=True)[:N]
)
print(f"tagged_items in db: {TaggedItem.objects.count():,}  |  probing {len(tagged_ids)} tagged, {len(untagged_ids)} untagged")

def bulk_patch(ids, label):
    payload = [{"id": str(i), "description": f"probe-{label}"} for i in ids]
    with CaptureQueriesContext(connection) as ctx:
        r = client.patch("/api/dcim/interfaces/", payload, format="json", SERVER_NAME="localhost")
    sqls = [q["sql"] for q in ctx.captured_queries]
    tagq = [s for s in sqls if "extras_taggeditem" in s.lower()]
    # a batch read filters on many ids; a per-object read does not
    batched = [s for s in tagq if " IN (" in s.upper() and s.upper().count(",") > 5]
    print(f"  {label:9} HTTP {r.status_code}  {len(sqls):5d} queries, {len(tagq):4d} touch extras_taggeditem "
          f"({len(batched)} look batched)")
    return {"status": r.status_code, "queries": len(sqls), "tag_queries": len(tagq), "batched": len(batched)}

out = {"tagged": bulk_patch(tagged_ids, "tagged"), "untagged": bulk_patch(untagged_ids, "untagged")}
print("JSON " + json.dumps(out))
