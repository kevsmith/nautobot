import django_tables2 as tables

from nautobot.core.tables import (
    BaseTable,
    BooleanColumn,
    ButtonsColumn,
    ContentTypesColumn,
    TagColumn,
    TemplateColumn,
    ToggleColumn,
)
from nautobot.dcim.models import Location, LocationType
from nautobot.dcim.tables.template_code import LOCATION_TREE_LINK, TREE_LINK
from nautobot.extras.tables import StatusTableMixin
from nautobot.tenancy.tables import TenantColumn

__all__ = (
    "LocationTable",
    "LocationTypeTable",
)


class LocationTypeTable(BaseTable):
    pk = ToggleColumn()
    name = TemplateColumn(template_code=TREE_LINK, attrs={"td": {"class": "text-nowrap"}})
    parent = tables.Column(linkify=True)
    nestable = BooleanColumn()
    content_types = ContentTypesColumn(truncate_words=15)
    actions = ButtonsColumn(LocationType)

    class Meta(BaseTable.Meta):
        model = LocationType
        fields = (
            "pk",
            "name",
            "parent",
            "nestable",
            "content_types",
            "description",
            "actions",
        )
        default_columns = (
            "pk",
            "name",
            "nestable",
            "content_types",
            "description",
            "actions",
        )


class LocationTable(StatusTableMixin, BaseTable):
    def paginate(self, *args, **kwargs):
        """Batch-resolve `children_exists` for the rows on this page.

        `LOCATION_TREE_LINK` renders an expand arrow per row, which asked each Location whether it
        had children -- one `EXISTS` query per rendered row. One query for the page answers it for
        every row, and seeding `__dict__` is what the `cached_property` reads.
        """
        paginated = super().paginate(*args, **kwargs)
        records = [getattr(row, "record", row) for row in self.page.object_list]
        records = [record for record in records if isinstance(record, Location)]
        parents_with_children = set(
            Location.objects.filter(parent__in=[record.pk for record in records])
            .values_list("parent_id", flat=True)
            .distinct()
        )
        for record in records:
            record.__dict__["children_exists"] = record.pk in parents_with_children
        return paginated

    pk = ToggleColumn()
    name = TemplateColumn(
        template_code=LOCATION_TREE_LINK,
        attrs={"td": {"class": "nb-tree-element text-nowrap", "data-pk": lambda record: str(record.pk)}},
    )
    location_type = tables.Column(linkify=True)
    parent = tables.Column(linkify=True)
    tenant = TenantColumn()
    tags = TagColumn(url_name="dcim:location_list")
    actions = ButtonsColumn(Location)

    class Meta(BaseTable.Meta):
        model = Location
        fields = (
            "pk",
            "name",
            "status",
            "location_type",
            "parent",
            "tenant",
            "description",
            "facility",
            "asn",
            "time_zone",
            "physical_address",
            "shipping_address",
            "latitude",
            "longitude",
            "contact_name",
            "contact_phone",
            "contact_email",
            "tags",
            "actions",
        )
        default_columns = ("pk", "name", "status", "parent", "tenant", "description", "tags", "actions")
