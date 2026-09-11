from collections import OrderedDict
import logging
import uuid

from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import URLValidator
from django.db.models import Model
from django.urls import NoReverseMatch
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.fields import URLField
from rest_framework.relations import PrimaryKeyRelatedField, RelatedField
from timezone_field.rest_framework import TimeZoneSerializerField as TimeZoneSerializerField_

from nautobot.core.api.mixins import WritableSerializerMixin
from nautobot.core.models.utils import deconstruct_composite_key
from nautobot.core.models.validators import EnhancedURLValidator
from nautobot.core.utils.data import is_url, is_uuid
from nautobot.core.utils.lookup import get_route_for_model

logger = logging.getLogger(__name__)


# TODO: why is this not a serializers.ChoiceField subclass??
class ChoiceField(serializers.Field):
    """
    Represent a ChoiceField as {'value': <DB value>, 'label': <string>}. Accepts a single value on write.

    Args:
        choices (Iterable): An iterable of choices in the form (value, key).
        allow_blank (bool): Allow blank values in addition to the listed choices.
    """

    def __init__(self, choices, allow_blank=False, **kwargs):
        self.choiceset = choices
        self.allow_blank = allow_blank
        self._choices = {}

        # Unpack grouped choices
        for k, v in choices:
            if isinstance(v, (list, tuple)):
                for k2, v2 in v:
                    self._choices[k2] = v2
            else:
                self._choices[k] = v

        super().__init__(**kwargs)

    def validate_empty_values(self, data):
        # Convert null to an empty string unless allow_null == True
        if data is None:
            if self.allow_null:
                return True, None
            else:
                data = ""
        return super().validate_empty_values(data)

    def to_representation(self, value):
        if value == "" and "" not in self._choices:
            return None
        return OrderedDict([("value", value), ("label", self._choices[value])])

    def to_internal_value(self, data):
        if data == "":
            if self.allow_blank:
                return data
            raise ValidationError("This field may not be blank.")

        if isinstance(data, dict):
            if "value" in data:
                data = data["value"]
            else:
                raise ValidationError(
                    'Value must be passed directly (e.g. "foo": 123) '
                    'or as a dict with key "value" (e.g. "foo": {"value": 123}).'
                )

        # Provide an explicit error message if the request is trying to write a dict or list
        if isinstance(data, list):
            raise ValidationError('Value must be passed directly (e.g. "foo": 123); do not use a list.')

        # Check for string representations of boolean/integer values
        if hasattr(data, "lower"):
            if data.lower() == "true":
                data = True
            elif data.lower() == "false":
                data = False
            else:
                try:
                    data = int(data)
                except ValueError:
                    pass

        try:
            if data in self._choices:
                return data
        except TypeError:  # Input is an unhashable type
            pass

        raise ValidationError(f"{data} is not a valid choice.")

    @property
    def choices(self):
        return self._choices


@extend_schema_field(str)
class ContentTypeField(RelatedField):
    """
    Represent a ContentType as '<app_label>.<model>'
    """

    default_error_messages = {
        "does_not_exist": "Invalid content type: {content_type}",
        "invalid": "Invalid value. Specify a content type as '<app_label>.<model_name>'.",
    }

    def to_internal_value(self, data):
        try:
            app_label, model = data.split(".")
            return self.queryset.get(app_label=app_label, model=model)
        except ObjectDoesNotExist:
            self.fail("does_not_exist", content_type=data)
        except (AttributeError, TypeError, ValueError):
            self.fail("invalid")
        return None

    def to_representation(self, value):
        return f"{value.app_label}.{value.model}"


@extend_schema_field(str)
class GroupField(RelatedField):
    """
    Represent a Group as its name
    """

    default_error_messages = {
        "does_not_exist": "Group cannot be found using primary key or name {group}",
        "invalid": "Invalid value. Specify a group using its primary key or name",
    }

    def to_internal_value(self, data):
        try:
            if isinstance(data, str):
                return self.queryset.get(name=data)
            else:
                return self.queryset.get(pk=data)
        except ObjectDoesNotExist:
            self.fail("does_not_exist", group=data)
        except (AttributeError, TypeError, ValueError):
            self.fail("invalid")
        return None

    def to_representation(self, value):
        return value.name


class LaxURLField(URLField):
    def __init__(self, validators=None, **kwargs):
        super().__init__(**kwargs)
        # Discard default URLValidator added by URLField
        self.validators = [v for v in self.validators if not isinstance(v, URLValidator)]
        if validators is not None:
            self.validators.extend(validators)
        self.validators.append(EnhancedURLValidator(message=self.error_messages["invalid"]))


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "id": {
                "oneOf": [
                    {"type": "string", "format": "uuid"},
                    {"type": "integer"},
                ]
            },
            "object_type": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]+\\.[a-z][a-z0-9_]+$",
                "example": "app_label.modelname",
            },
            "url": {
                "type": "string",
                "format": "uri",
            },
        },
    }
)
class HyperlinkedURLMemoMixin:
    """Memoize the *shape* of a hyperlinked URL so `reverse()` runs once per route per request.

    Reversing a URL costs ~51us (finding 51), and a hyperlinked serializer field reverses the same
    route once for every object in the page: a 100-row Device list reversed
    `extras-api:status-detail` 100 times for 100 identical routes differing only in the pk.

    The route shape is resolved once with a sentinel UUID, split around it, and the pk substituted
    for every later object. The memo lives on the field instance, which DRF creates per serializer
    instantiation and reuses for every object under `many=True`, so it is request-scoped and cannot
    outlive a change of host or script prefix. Routes whose lookup is not a UUID pk -- `ContentType`
    and `Group` have integer pks -- raise `NoReverseMatch` on the sentinel and fall back to DRF's
    per-object implementation permanently.
    """

    _url_memo = None

    def get_url(self, obj, view_name, request, format):  # noqa: A002  # shadows builtin, DRF's signature
        # Unsaved objects have no URL yet; this is DRF's own short-circuit.
        if hasattr(obj, "pk") and obj.pk in (None, ""):
            return None
        # Only a plain pk lookup with no format suffix has a shape constant enough to reuse.
        if format is not None or self.lookup_field != "pk" or self.lookup_url_kwarg != "pk":
            return super().get_url(obj, view_name, request, format)

        if self._url_memo is None:
            self._url_memo = {}
        shape = self._url_memo.get(view_name, "")
        if shape == "":
            sentinel = uuid.uuid4()
            try:
                url = self.reverse(view_name, kwargs={"pk": sentinel}, request=request)
                prefix, found, suffix = url.partition(str(sentinel))
                shape = (prefix, suffix) if found else None
            except NoReverseMatch:
                shape = None
            self._url_memo[view_name] = shape
        if shape is None:
            return super().get_url(obj, view_name, request, format)
        prefix, suffix = shape
        return f"{prefix}{obj.pk}{suffix}"


class NautobotHyperlinkedIdentityField(HyperlinkedURLMemoMixin, serializers.HyperlinkedIdentityField):
    """The `url` field of every serializer, with the same per-request route-shape memo."""


class NautobotHyperlinkedRelatedField(HyperlinkedURLMemoMixin, WritableSerializerMixin, serializers.HyperlinkedRelatedField):
    """
    Extend HyperlinkedRelatedField to include URL namespace-awareness, add 'object_type' field, and read composite-keys.
    """

    def __init__(self, *args, **kwargs):
        """Override DRF's namespace-unaware default view_name logic for HyperlinkedRelatedField.

        DRF defaults to '{model_name}-detail' instead of '{app_label}:{model_name}-detail'.
        """
        if "view_name" not in kwargs or (kwargs["view_name"].endswith("-detail") and ":" not in kwargs["view_name"]):
            if "queryset" not in kwargs:
                logger.warning(
                    '"view_name=%r" is probably incorrect for this related API field; '
                    'unable to determine the correct "view_name" as "queryset" wasn\'t specified',
                    kwargs["view_name"],
                )
            else:
                kwargs["view_name"] = get_route_for_model(kwargs["queryset"].model, "detail", api=True)
        super().__init__(*args, **kwargs)

    @property
    def _related_model(self):
        """The model class that this field is referencing to."""
        if self.queryset is not None:
            return self.queryset.model
        # Foreign key where the destination is referenced by string rather than by Python class
        if getattr(self.parent.Meta.model, self.source, False):
            return getattr(self.parent.Meta.model, self.source).field.model

        logger.warning(
            "Unable to determine model for related field %r; "
            "ensure that either the field defines a 'queryset' or the Meta defines the related 'model'.",
            self.field_name,
        )
        return None

    def to_internal_value(self, data):
        """Convert potentially nested representation to a model instance."""
        if isinstance(data, dict):
            if "url" in data:
                return super().to_internal_value(data["url"])
            elif "id" in data:
                return super().to_internal_value(data["id"])
        if isinstance(data, str) and not is_uuid(data) and not is_url(data):
            # Maybe it's a composite-key?
            related_model = self._related_model
            if related_model is not None and hasattr(related_model, "natural_key_args_to_kwargs"):
                try:
                    data = related_model.natural_key_args_to_kwargs(deconstruct_composite_key(data))
                except ValueError as err:
                    # Not a correctly constructed composite key?
                    raise ValidationError(f"Related object not found using provided composite-key: {data}") from err
            elif related_model is not None and related_model.label_lower == "auth.group":
                # auth.Group is a base Django model and so doesn't implement our natural_key_args_to_kwargs() method
                data = {"name": deconstruct_composite_key(data)}
        return super().to_internal_value(data)

    def to_representation(self, value):
        """Convert URL representation to a brief nested representation."""
        url = super().to_representation(value)

        # nested serializer provides an instance
        if isinstance(value, Model):
            model = type(value)
        else:
            model = self._related_model

        if model is None:
            return {"id": value.pk, "object_type": "unknown.unknown", "url": url}
        return {"id": value.pk, "object_type": model._meta.label_lower, "url": url}


@extend_schema_field(
    {
        "type": "string",
        "pattern": "^[a-z][a-z0-9_]+\\.[a-z][a-z0-9_]+$",
        "example": "app_label.modelname",
    }
)
class ObjectTypeField(serializers.CharField):
    """
    Represent the ContentType of this serializer's model as "<app_label>.<model>".
    """

    def __init__(self, *args, read_only=True, source="*", **kwargs):  # pylint: disable=useless-parent-delegation
        """Default read_only to True as this should never be a writable field."""
        super().__init__(*args, read_only=read_only, source=source, **kwargs)

    def to_representation(self, _value):
        """
        Get the content-type of this serializer's model.

        Implemented this way because `_value` may be None when generating the schema.
        """
        model = self.parent.Meta.model
        return model._meta.label_lower


class SerializedPKRelatedField(PrimaryKeyRelatedField):
    """
    Extends PrimaryKeyRelatedField to return a serialized object on read. This is useful for representing related
    objects in a ManyToManyField while still allowing a set of primary keys to be written.
    """

    def __init__(self, serializer, **kwargs):
        self.serializer = serializer
        self.pk_field = kwargs.pop("pk_field", None)
        super().__init__(**kwargs)

    def to_representation(self, value):
        return self.serializer(value, context={"request": self.context["request"]}).data


@extend_schema_field(str)
class TimeZoneSerializerField(TimeZoneSerializerField_):
    """Represents a time zone as a string."""
