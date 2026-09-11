#!/usr/bin/env python
"""Does anything read the `_tags` cache that `tag_cache_warmed` populates?

`tag_cache_warmed` (finding 31) runs only under `defer_object_changes`, which only
`bulk_delete_with_bulk_change_logging` sets. Inside that loop `to_objectchange` is called --
and finding 22 removed `serialize_object` from it, leaving `serialize_object_v2`, which does
not read `_tags`. Rolled back; nothing is actually deleted.
"""
import json
import os

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.db import connection, transaction  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from nautobot.core.models import utils as mutils  # noqa: E402
from nautobot.dcim.models import Interface  # noqa: E402
from nautobot.extras.context_managers import change_logging, web_request_context  # noqa: E402
from nautobot.extras.models import TaggedItem  # noqa: E402
from nautobot.extras.utils import bulk_delete_with_bulk_change_logging  # noqa: E402
from nautobot.users.models import User  # noqa: E402

counts = {"serialize_object": 0, "serialize_object_v2": 0, "tag_cache_warmed": 0, "tags_read_via_getattr": 0}

_so = getattr(mutils, "serialize_object", None)
if _so:
    def c_so(*a, **k):
        counts["serialize_object"] += 1
        return _so(*a, **k)
    mutils.serialize_object = c_so
_so2 = mutils.serialize_object_v2
def c_so2(*a, **k):
    counts["serialize_object_v2"] += 1
    return _so2(*a, **k)
mutils.serialize_object_v2 = c_so2
from nautobot.extras.models import change_logging as cl  # noqa: E402
if hasattr(cl, "serialize_object"):
    cl.serialize_object = c_so
if hasattr(cl, "serialize_object_v2"):
    cl.serialize_object_v2 = c_so2

_warm = getattr(mutils, "tag_cache_warmed", None)
from nautobot.extras import context_managers as cm  # noqa: E402
if _warm is not None:
    def c_warm(objects):
        counts["tag_cache_warmed"] += 1
        return _warm(objects)
    mutils.tag_cache_warmed = c_warm
    if hasattr(cm, "tag_cache_warmed"):
        cm.tag_cache_warmed = c_warm
else:
    counts["tag_cache_warmed"] = "absent (stock)"

user = User.objects.filter(is_superuser=True).first()
ids = list(TaggedItem.objects.filter(content_type__model="interface").values_list("object_id", flat=True).distinct()[:40])
qs = Interface.objects.filter(pk__in=ids)
print(f"  deleting {qs.count()} tagged interfaces (rolled back)")

try:
    with transaction.atomic():
        # Capture around the WHOLE request context: the deferred change-log flush runs on exit,
        # which is where tag_cache_warmed lives. Measuring inside the block misses it entirely.
        with CaptureQueriesContext(connection) as ctx:
            with web_request_context(user, context_detail="tag-cache-probe"):
                bulk_delete_with_bulk_change_logging(qs, batch_size=1000)
        sqls = [q["sql"] for q in ctx.captured_queries]
        tagq = [s for s in sqls if "extras_taggeditem" in s.lower()]
        print(f"  queries {len(sqls)}, taggeditem queries {len(tagq)}")
        for k, v in counts.items():
            print(f"    {k:24} {v}")
        print("JSON " + json.dumps({**counts, "queries": len(sqls), "tag_queries": len(tagq)}))
        raise RuntimeError("rollback")
except RuntimeError as e:
    if str(e) != "rollback":
        raise
    print("  rolled back")
