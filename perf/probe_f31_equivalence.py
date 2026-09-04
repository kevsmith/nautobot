#!/usr/bin/env python
"""Does finding 31 change what change logging records? Run in both arms and diff the output.

The change makes `_tags` a cache that fires when it is empty, so every path that sets it -- and
every path that does not -- has to record the same tags as before. This walks the paths that can
differ: inline create, deferred create, tags added after create, an ORM update of a tagged
object, a REST PATCH that omits tags, a REST PATCH that clears them, and a delete.

Everything runs inside one rolled-back transaction, so the dataset is untouched.

    perf/dc.sh exec -T nautobot python /source/perf/probe_f31_equivalence.py
"""

import os

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test.client import RequestFactory  # noqa: E402
from rest_framework.request import Request  # noqa: E402

from nautobot.dcim.api.serializers import InterfaceSerializer  # noqa: E402
from nautobot.dcim.models import Device, Interface  # noqa: E402
from nautobot.extras.context_managers import deferred_change_logging_for_bulk_operation, web_request_context  # noqa: E402
from nautobot.extras.models import ObjectChange, Status, Tag  # noqa: E402


class Rollback(Exception):
    pass


def api_context():
    """The serializer context a REST view would supply; core serializers read request.data."""
    request = Request(RequestFactory().patch("/api/dcim/interfaces/"))
    request.user = user
    return {"request": request, "depth": 0}


def changes_for(marker):
    """Every ObjectChange recorded for objects whose repr carries `marker`, oldest first."""
    rows = []
    for oc in ObjectChange.objects.filter(object_repr__contains=marker).order_by("time", "object_repr"):
        rows.append((oc.object_repr, oc.action, sorted(oc.object_data.get("tags", []))))
    return rows


def report(label, marker):
    for repr_, action, tags in changes_for(marker):
        print(f"  {label:26s} {action:6s} {repr_:26s} tags={tags}")


user_model = get_user_model()
user = user_model.objects.filter(username="perf-probe").first()
if user is None:
    user = user_model.objects.create(username="perf-probe", is_superuser=True, is_staff=True, is_active=True)

try:
    with transaction.atomic():
        device = Device.objects.order_by("pk").first()
        status = Status.objects.get_for_model(Interface).order_by("pk").first()
        interface_ct = ContentType.objects.get_for_model(Interface)

        red = Tag.objects.create(name="f31-red")
        blue = Tag.objects.create(name="f31-blue")
        for tag in (red, blue):
            tag.content_types.add(interface_ct)

        def new_interface(name):
            return Interface(device=device, name=name, type="1000base-t", status=status)

        # 1. inline create, no tags
        with web_request_context(user, context_detail="f31"):
            new_interface("f31-a-plain").validated_save()

        # 2. inline create, then tags added afterwards
        with web_request_context(user, context_detail="f31"):
            iface = new_interface("f31-b-tagged-after")
            iface.validated_save()
            iface.tags.set([red, blue])

        # 3. deferred create, no tags
        with web_request_context(user, context_detail="f31"):
            with deferred_change_logging_for_bulk_operation():
                for i in range(2):
                    new_interface(f"f31-c-deferred-{i}").validated_save()

        # 4. deferred create with tags assigned before the flush
        with web_request_context(user, context_detail="f31"):
            with deferred_change_logging_for_bulk_operation():
                iface = new_interface("f31-d-deferred-tagged")
                iface.validated_save()
                iface.tags.set([red])

        # 5. ORM update of an object that already has tags
        target = Interface.objects.get(device=device, name="f31-b-tagged-after")
        with web_request_context(user, context_detail="f31"):
            target.description = "f31-e-orm-update"
            target.validated_save()

        # 6. REST PATCH that does not mention tags, on a tagged object
        patched = Interface.objects.get(device=device, name="f31-b-tagged-after")
        with web_request_context(user, context_detail="f31"):
            serializer = InterfaceSerializer(
                patched, data={"label": "f31-f-patch-no-tags"}, partial=True, context=api_context()
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()

        # 7. REST PATCH that clears tags on a tagged object
        cleared = Interface.objects.get(device=device, name="f31-b-tagged-after")
        with web_request_context(user, context_detail="f31"):
            serializer = InterfaceSerializer(cleared, data={"tags": []}, partial=True, context=api_context())
            serializer.is_valid(raise_exception=True)
            serializer.save()

        # 8. delete a tagged object
        doomed = new_interface("f31-g-doomed")
        doomed.validated_save()
        doomed.tags.set([blue])
        with web_request_context(user, context_detail="f31"):
            doomed.delete()

        print("ObjectChange.object_data['tags'] by scenario, oldest change first\n")
        report("1 inline create", "f31-a-plain")
        report("2 tags added after create", "f31-b-tagged-after")
        report("3 deferred create", "f31-c-deferred")
        report("4 deferred + tags", "f31-d-deferred-tagged")
        report("8 delete tagged", "f31-g-doomed")

        raise Rollback
except Rollback:
    print("\nrolled back.")
