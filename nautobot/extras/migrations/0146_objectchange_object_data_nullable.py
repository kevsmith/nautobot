# Schema migration: allow ObjectChange.object_data to be NULL.
#
# object_data holds the legacy v1 snapshot -- Django's serialize("json"), a flat dump of the
# object's own columns with foreign keys as bare primary keys. object_data_v2 holds the
# representation every current consumer reads: the DRF API serializer at depth 1.
#
# Both were written for every change record, so each write paid for two serializations. This
# makes the v1 column nullable so new records can stop populating it. Existing rows keep their
# snapshot, and get_snapshots() continues to read v1 where that is all a record has.
#
# Metadata-only: AlterField to null=True does not rewrite the table.

from django.core.serializers.json import DjangoJSONEncoder
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("extras", "0145_objectmetadata_assigned_object_type_cascade"),
    ]

    operations = [
        migrations.AlterField(
            model_name="objectchange",
            name="object_data",
            field=models.JSONField(blank=True, editable=False, encoder=DjangoJSONEncoder, null=True),
        ),
    ]
