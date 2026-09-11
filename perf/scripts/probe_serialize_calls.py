#!/usr/bin/env python
"""Who actually calls serialize_object during a change-logged write, and does anyone read `_tags`?

Finding 31 optimised `serialize_object`'s `_tags` cache. Finding 22 removed the
`serialize_object` call from `to_objectchange`. If nothing calls it, `tag_cache_warmed` is
issuing a batch query whose result nobody reads.
"""
import json
import os

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from rest_framework.test import APIClient  # noqa: E402

from nautobot.core.models import utils as mutils  # noqa: E402
from nautobot.extras.models import TaggedItem  # noqa: E402
from nautobot.users.models import Token  # noqa: E402

counts = {"serialize_object": 0, "serialize_object_v2": 0, "tag_cache_warmed": 0, "_tags_read": 0}

_so = mutils.serialize_object
def counting_so(*a, **k):
    counts["serialize_object"] += 1
    return _so(*a, **k)
mutils.serialize_object = counting_so

_so2 = mutils.serialize_object_v2
def counting_so2(*a, **k):
    counts["serialize_object_v2"] += 1
    return _so2(*a, **k)
mutils.serialize_object_v2 = counting_so2

_warm = getattr(mutils, "tag_cache_warmed", None)   # branch-only: finding 31 added it
if _warm is not None:
    def counting_warm(objects):
        counts["tag_cache_warmed"] += 1
        return _warm(objects)
    mutils.tag_cache_warmed = counting_warm
else:
    counting_warm = None
    counts["tag_cache_warmed"] = "absent (stock)"

# extras.context_managers imported the name directly, so rebind it there too.
from nautobot.extras import context_managers as cm  # noqa: E402
if counting_warm is not None and hasattr(cm, "tag_cache_warmed"):
    cm.tag_cache_warmed = counting_warm
from nautobot.extras.models import change_logging as cl  # noqa: E402
if hasattr(cl, "serialize_object"):
    cl.serialize_object = counting_so
if hasattr(cl, "serialize_object_v2"):
    cl.serialize_object_v2 = counting_so2

token = Token.objects.filter(key="0123456789abcdef0123456789abcdef01234567").first()
client = APIClient()
client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
ids = list(TaggedItem.objects.filter(content_type__model="interface").values_list("object_id", flat=True).distinct()[:50])
payload = [{"id": str(i), "description": "probe-calls"} for i in ids]
r = client.patch("/api/dcim/interfaces/", payload, format="json", SERVER_NAME="localhost")
print(f"  HTTP {r.status_code}, {len(ids)} tagged interfaces patched")
for k, v in counts.items():
    print(f"    {k:22} {v}")
print("JSON " + json.dumps(counts))
