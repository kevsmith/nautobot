from django.db.models import QuerySet
import django_tables2 as tables
from django_tables2.utils import Accessor

from nautobot.core.tables import (
    BaseTable,
    BooleanColumn,
    ButtonsColumn,
    ColorColumn,
    LinkedCountColumn,
    TagColumn,
    TemplateColumn,
    ToggleColumn,
)
from nautobot.dcim.models import Cable, CableType
from nautobot.extras.tables import StatusTableMixin

from .template_code import CABLE_LENGTH, CABLE_TERMINATIONS_MULTI

__all__ = ("CableTable", "CableTypeTable")


#
# Cables
#


class CableTypeTable(BaseTable):
    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    manufacturer = tables.Column(linkify=True)
    has_embedded_transceivers = BooleanColumn()
    is_shuffle = BooleanColumn()
    total_strands = tables.Column(orderable=False)
    is_breakout = BooleanColumn(orderable=False)
    cable_count = LinkedCountColumn(
        viewname="dcim:cable_list",
        url_params={"cable_type": "pk"},
        verbose_name="Cables",
    )
    tags = TagColumn(url_name="dcim:cabletype_list")
    actions = ButtonsColumn(CableType)

    class Meta(BaseTable.Meta):
        model = CableType
        fields = (
            "pk",
            "name",
            "description",
            "manufacturer",
            "part_number",
            "a_connectors",
            "b_connectors",
            "total_lanes",
            "has_embedded_transceivers",
            "is_shuffle",
            "strands_per_lane",
            "polarity_method",
            "total_strands",
            "is_breakout",
            "cable_count",
            "tags",
            "actions",
        )
        default_columns = (
            "pk",
            "name",
            "manufacturer",
            "part_number",
            "a_connectors",
            "b_connectors",
            "total_lanes",
            "cable_count",
            "tags",
            "actions",
        )


class CableTable(StatusTableMixin, BaseTable):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The termination columns resolve each cable's terminations through the join table via
        # properties the accessor walk cannot see. Apply the model's optimization here so the table
        # is correct wherever it renders, not only on the list view that remembered to do it. A
        # Prefetch re-added for an already-prefetched path raises at evaluation, hence the check.
        if isinstance(self.data.data, QuerySet):
            already_prefetched = {
                lookup if isinstance(lookup, str) else lookup.prefetch_to
                for lookup in self.data.data._prefetch_related_lookups
            }
            if "terminations" in already_prefetched:
                self.replace_queryset(self.data.data.select_related("cable_type"))
            else:
                self.replace_queryset(Cable.optimize_queryset_for_cable_columns(self.data.data))

    pk = ToggleColumn()
    id = tables.Column(linkify=True, verbose_name="ID")
    cable_type = tables.Column(linkify=True, verbose_name="Cable Type")
    termination_a_parent = tables.Column(
        linkify=True,
        accessor=Accessor("termination_a__parent"),
        orderable=False,
        verbose_name="Termination A Parent",
    )
    termination_a = tables.Column(
        linkify=True,
        accessor=Accessor("termination_a"),
        orderable=False,
        verbose_name="Termination A",
    )
    terminations_a = TemplateColumn(
        template_code=CABLE_TERMINATIONS_MULTI,
        accessor=Accessor("get_connections_a"),
        orderable=False,
        verbose_name="A-Side Terminations",
    )
    termination_b_parent = tables.Column(
        linkify=True,
        accessor=Accessor("termination_b__parent"),
        orderable=False,
        verbose_name="Termination B Parent",
    )
    termination_b = tables.Column(
        linkify=True,
        accessor=Accessor("termination_b"),
        orderable=False,
        verbose_name="Termination B",
    )
    terminations_b = TemplateColumn(
        template_code=CABLE_TERMINATIONS_MULTI,
        accessor=Accessor("get_connections_b"),
        orderable=False,
        verbose_name="B-Side Terminations",
    )
    length = TemplateColumn(template_code=CABLE_LENGTH, order_by="_abs_length")
    color = ColorColumn()
    tags = TagColumn(url_name="dcim:cable_list")
    actions = ButtonsColumn(Cable)

    class Meta(BaseTable.Meta):
        model = Cable
        fields = (
            "pk",
            "id",
            "label",
            "cable_type",
            "termination_a_parent",
            "termination_a",
            "terminations_a",
            "termination_b_parent",
            "termination_b",
            "terminations_b",
            "status",
            "type",
            "color",
            "length",
            "tags",
            "actions",
        )
        default_columns = (
            "pk",
            "id",
            "label",
            "terminations_a",
            "terminations_b",
            "status",
            "type",
            "actions",
        )
