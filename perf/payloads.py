#!/usr/bin/env python
"""Generate a minimal schema-valid REST payload for any Nautobot model.

This is the half of the write screening matrix that can lie. The read screen
cannot silently under-report: every list endpoint answers, and an endpoint that
returns nothing is visibly a zero. A write screen has a failure mode the read
screen does not -- a model whose generated payload is rejected drops out of the
run and reads as *not a problem* when the truth is *not measured*. So every
function here returns a reason alongside its result, and the reason is what the
coverage accounting in ``screen_writes.py`` reports.

**Where the field information comes from.** DRF's OPTIONS metadata is built by
walking ``serializer.fields`` (``rest_framework/metadata.py``), so the OPTIONS
document and this module read the same source. This reads it directly rather
than over HTTP: same information, no round trip, and the field *objects* are
available rather than their JSON projection -- which matters, because a related
field's ``queryset`` and the model field's ``limit_choices_to`` are what make it
possible to pick a value that will actually validate.

**Minimal, deliberately.** Only fields DRF reports as required and writable are
populated. That measures the *floor* cost of a create. Optional fields that cost
real write work -- tags, custom field data, relationships -- are omitted, so a
per-model number here is a lower bound and not an estimate of what a populated
create costs. ``--include-optional`` on the screen turns the scalar ones on for
comparison; it is off by default because optional related fields are where
cross-field validation lives and turning them on trades coverage for realism.

**Deterministic.** Related values are resolved by ``order_by("pk").first()`` over
a filtered queryset, never at random, so two runs against the same dataset build
byte-identical payloads and a payload that validates today validates tomorrow.
"""

import datetime
import decimal

from django.core.exceptions import FieldDoesNotExist
from django.db.models import Q
from rest_framework import serializers as drf

from nautobot.core.api import fields as nb_fields

# Fields that are writable but that we never populate. Each one is here for a
# reason that is not "it looked hard".
NEVER_POPULATE = {
    # Writable, always optional, and populating them measures the cost of the
    # extras subsystem rather than the cost of the model's own write path. They
    # are exactly what --include-optional should NOT turn on.
    "custom_fields",
    "relationships",
    "tags",
    "notes",
    # Password-ish and other write-only credentials on a handful of models.
    "password",
}

_TEXT_SEED = "perfw"


# --- the hand-maintained exception list --------------------------------------
#
# Everything above derives payloads from field metadata alone. These are the
# models where metadata is genuinely not enough, and the list is deliberately
# small, explicit, and counted: `screen_writes.py` reports how many measured
# models needed an entry, so "we measured 90 models" can never quietly mean "we
# measured 70 models and hand-fed 20 others until they passed".
#
# Two kinds of knowledge live here and nothing else may:
#
# `force`   names *optional* fields that must be populated anyway. It supplies no
#           values -- the ordinary generator still produces them. This covers the
#           largest single class of unmeasurable models: a `clean()` that requires
#           exactly one of several optional foreign keys ("Either device or module
#           must be set"), which no amount of field introspection can predict.
#
# `values`  supplies a value the generator cannot invent. Only IPAM needs this,
#           and only because an address has to fall inside an existing prefix in
#           the right namespace -- a fact about the dataset's contents, not about
#           the field.
#
# A model is NOT added here to make a number look better. It is added when the
# constraint is real, external to the field definitions, and the model is worth
# measuring. Everything else stays in the coverage report as unmeasured.

MODEL_SEEDS = {
    # "Either device or module must be set" -- device components.
    "dcim.consoleport": {"force": ["device"]},
    "dcim.consoleserverport": {"force": ["device"]},
    "dcim.interface": {"force": ["device"]},
    "dcim.powerport": {"force": ["device"]},
    "dcim.poweroutlet": {"force": ["device"]},
    "dcim.rearport": {"force": ["device"]},
    "dcim.modulebay": {"force": ["parent_device"]},
    # "Either device_type or module_type must be set" -- the component templates.
    "dcim.consoleporttemplate": {"force": ["device_type"]},
    "dcim.consoleserverporttemplate": {"force": ["device_type"]},
    "dcim.interfacetemplate": {"force": ["device_type"]},
    "dcim.powerporttemplate": {"force": ["device_type"]},
    "dcim.poweroutlettemplate": {"force": ["device_type"]},
    "dcim.rearporttemplate": {"force": ["device_type"]},
    "dcim.modulebaytemplate": {"force": ["device_type"]},
    # One-of-several assignments.
    "extras.contactassociation": {"force": ["contact"]},
    "ipam.ipaddresstointerface": {"force": ["interface"]},
    "ipam.vrfdeviceassignment": {"force": ["device"]},
    "vpn.vpntermination": {"force": ["interface"]},
    "vpn.vpntunnelendpoint": {"force": ["source_interface"]},
    "ipam.service": {"force": ["device"]},
    # IPAM addresses have to land inside real prefixes in the right namespace.
    "ipam.prefix": {"values": {"prefix": "_seed_prefix"}},
    # `namespace` is in `force` as well as `values`: it is a write-only,
    # not-required serializer field backed by no model field, so nothing else
    # would populate it -- and IPAddress.validate() rejects a create that carries
    # neither a namespace nor a parent.
    "ipam.ipaddress": {
        "force": ["namespace"],
        "values": {"address": "_seed_address", "namespace": "_seed_namespace"},
    },
    "ipam.ipaddressrange": {
        "force": ["namespace"],
        "values": {
            "start_address": "_seed_range_start",
            "end_address": "_seed_range_end",
            "namespace": "_seed_namespace",
        },
    },
}


class Unbuildable(Exception):
    """No value could be produced for a required field."""

    def __init__(self, field_name, reason):
        self.field_name = field_name
        self.reason = reason
        super().__init__(f"{field_name}: {reason}")


class PayloadBuilder:
    """Builds create/update payloads, and explains itself when it cannot.

    One instance per run. It caches related-object lookups, because the same
    Status/Role/Location gets resolved for dozens of models and each resolution
    is a query.
    """

    def __init__(self, include_optional=False, seed=_TEXT_SEED):
        self.include_optional = include_optional
        self.seed = seed
        self._related_cache = {}
        self._generic_cache = {}
        self._free_hosts = None
        self.seeded_models = set()

    # --- entry points -------------------------------------------------------

    def build_create(self, serializer_class, model, context, count=1, tag=""):
        """Return (payloads, diagnostics).

        ``count`` payloads are returned. Generated text varies by index so a bulk
        POST does not collide on unique names -- and so does any related field
        that participates in a uniqueness constraint, because a third of the
        write surface is assignment/through models whose uniqueness is over two
        foreign keys and nothing else. Ten payloads pointing at the same pair is
        an IntegrityError, which would be reported as a model whose bulk create
        cannot be measured when the truth is that the builder repeated itself.

        Raises ``Unbuildable`` if a required field has no derivable value -- which
        for ``count > 1`` includes "there are not ``count`` distinct rows to point
        at". A caller that still wants the single-object figure should build with
        ``count=1`` separately rather than treating the whole model as unmeasured.
        """
        serializer = serializer_class(context=context)
        fields = self._writable_fields(serializer)
        vary = self._uniqueness_fields(model, fields) if count > 1 else frozenset()
        seed = MODEL_SEEDS.get(model._meta.label_lower, {})
        forced = set(seed.get("force", ()))
        overrides = seed.get("values", {})
        if seed:
            self.seeded_models.add(model._meta.label_lower)
        diagnostics = {
            "fields_total": len(serializer.fields),
            "fields_writable": len(fields),
            "fields_populated": [],
            "fields_skipped": {},
        }

        payloads = []
        for i in range(count):
            payload = {}
            for name, field in fields.items():
                required = field.required or self._model_demands_a_value(model, field, name) or name in forced
                if not required and not self.include_optional:
                    continue
                try:
                    if name in overrides:
                        value = getattr(self, overrides[name])(model, i)
                    else:
                        value = self.value_for(name, field, model, index=i, tag=tag, vary=vary)
                except Unbuildable as exc:
                    if required:
                        diagnostics["fields_skipped"][name] = exc.reason
                        raise
                    diagnostics["fields_skipped"][name] = exc.reason
                    continue
                if value is _OMIT:
                    continue
                payload[name] = value
            payloads.append(payload)

        diagnostics["fields_populated"] = sorted(payloads[0]) if payloads else []
        return payloads, diagnostics

    def build_update(self, serializer_class, model, context):
        """Return (payload, field_name) for a PATCH, or raise Unbuildable.

        One field, the same field on every model wherever possible, so update
        costs are comparable across models rather than each measuring a
        different amount of work. ``description`` is present on nearly every
        Nautobot primary model and writing it touches no relations, so it
        isolates the fixed cost of the update path: validation, the save, and
        change logging over a one-field diff.
        """
        serializer = serializer_class(context=context)
        fields = self._writable_fields(serializer)
        for candidate in ("description", "comments", "label"):
            field = fields.get(candidate)
            if isinstance(field, drf.CharField) and not field.required:
                return {candidate: f"{self.seed}-update"}, candidate
        raise Unbuildable("<update>", "no writable description/comments/label field")

    # --- field walking ------------------------------------------------------

    @staticmethod
    def _writable_fields(serializer):
        return {
            name: field
            for name, field in serializer.fields.items()
            if not field.read_only and not isinstance(field, drf.HiddenField) and name not in NEVER_POPULATE
        }

    def _model_demands_a_value(self, model, field, name):
        """True when DRF says optional and the model says otherwise.

        DRF derives `required` from the *serializer*, and a model field with
        `blank=False` and no default is still `required=False` there whenever the
        column is nullable or the field is non-editable. `full_clean` then rejects
        the blank value the payload omitted. That gap is why configcontext,
        configcontextschema, secret, rackreservation and service were reported as
        rejected payloads: the builder was obeying the serializer and the model
        was overruling it.
        """
        model_field = self._model_field(model, field, name)
        if model_field is None or not hasattr(model_field, "blank"):
            return False
        if model_field.blank or model_field.null:
            return False
        return not model_field.has_default() and getattr(model_field, "editable", True)

    @classmethod
    def _uniqueness_fields(cls, model, fields):
        """Model field names a bulk payload must vary to keep its rows distinct.

        Constraints are read from the model rather than the serializer: DRF
        surfaces `unique_together` as a `UniqueTogetherValidator`, but `Meta` is
        the authority and also carries `UniqueConstraint`.

        A constraint is only forced onto its *relational* members when nothing
        else in it already varies. `DeviceType` is unique on
        (manufacturer, model) and `model` is generated text carrying the payload
        index, so ten payloads are already distinct -- forcing ten manufacturers
        as well would have made the model unmeasurable in bulk on a dataset that
        has fewer than ten, which is the screen inventing its own coverage gap.
        """
        by_source = {(field.source or name): field for name, field in fields.items()}
        groups = [tuple(group) for group in (getattr(model._meta, "unique_together", ()) or ())]
        for constraint in getattr(model._meta, "constraints", ()) or ():
            group = getattr(constraint, "fields", ()) or ()
            if group:
                groups.append(tuple(group))
        for field in model._meta.get_fields():
            if getattr(field, "unique", False) and not getattr(field, "primary_key", False):
                groups.append((field.name,))

        varying = set()
        for group in groups:
            if any(cls._varies_by_index(name, by_source) for name in group):
                continue
            varying.update(group)
        return frozenset(varying)

    @staticmethod
    def _varies_by_index(source, by_source):
        """True if the builder already gives this field a different value per payload.

        Every generated text value embeds the payload index, so one required text
        field in a constraint is enough to keep the group distinct.
        """
        field = by_source.get(source)
        return isinstance(field, drf.CharField) and field.required

    def value_for(self, name, field, model, index=0, tag="", vary=frozenset()):
        """Produce a value for one serializer field, or raise Unbuildable."""
        # Order matters: Nautobot's ChoiceField is a bare serializers.Field, and
        # ContentTypeField is a RelatedField whose value is a string, so both
        # have to be recognised before the generic branches below.
        if isinstance(field, nb_fields.ChoiceField):
            return self._first_choice(name, field.choices)
        # A generic foreign key arrives as two independent serializer fields, and
        # neither is answerable alone: the content type has to name a model that
        # has rows, and the id has to be a row of *that* model. Resolving them as
        # a pair is what makes the eight generic-FK models measurable at all.
        base = self._generic_base(name, field)
        if base is not None:
            # A generic FK in a uniqueness constraint needs a distinct target per
            # payload, same as an ordinary one. The content type stays fixed and
            # the object walks: varying the type instead would measure ten
            # different models' write paths under one model's name.
            distinct = {f"{base}_id", f"{base}_type"} & set(vary)
            content_type, object_id = self._generic_pair(base, model, field.parent, index if distinct else 0)
            return content_type if name.endswith("_type") else object_id
        if isinstance(field, nb_fields.ContentTypeField):
            return self._content_type_value(name, field, model)
        if isinstance(field, nb_fields.ObjectTypeField):
            return model._meta.label_lower
        if isinstance(field, nb_fields.TimeZoneSerializerField):
            return "UTC"
        if isinstance(field, drf.ManyRelatedField):
            if not field.required:
                return _OMIT
            # Dispatch on the *child*, not straight to a pk. `content_types` is a
            # many-related field over ContentTypeField, whose value is the string
            # 'app_label.model' -- handing it a UUID is rejected, which is what
            # made status, role, tag, webhook, customfield, metadatatype,
            # jobbutton, cloudresourcetype and objectpermission all unmeasurable.
            child = field.child_relation
            child.field_name = child.field_name or name
            child.parent = child.parent or field.parent
            return [self.value_for(name, child, model, index=index, tag=tag, vary=vary)]
        if isinstance(field, drf.RelatedField):
            distinct = self._is_unique_fk(model, field, name) or (field.source or name) in vary
            return self._related_value(name, field, model, unique=distinct, index=index)
        if isinstance(field, drf.ChoiceField):
            return self._first_choice(name, field.choices)
        if isinstance(field, drf.BaseSerializer):
            raise Unbuildable(name, f"nested writable serializer ({type(field).__name__})")

        return self._scalar_value(name, field, index=index, tag=tag)

    @staticmethod
    def _generic_base(name, field):
        """The shared prefix of a `<base>_type` / `<base>_id` pair, if this field is half of one."""
        parent = getattr(field, "parent", None)
        sibling_fields = getattr(parent, "fields", None)
        if sibling_fields is None:
            return None
        if isinstance(field, nb_fields.ContentTypeField) and name.endswith("_type"):
            base = name[: -len("_type")]
            return base if f"{base}_id" in sibling_fields else None
        if isinstance(field, drf.UUIDField) and name.endswith("_id"):
            base = name[: -len("_id")]
            return base if f"{base}_type" in sibling_fields else None
        return None

    def _generic_pair(self, base, model, serializer, index=0):
        """(content_type_label, object_pk) for one generic foreign key.

        Content types are walked in pk order and the first one whose model has a
        row wins, so the pair is always internally consistent and the choice is
        deterministic. Picking the first *content type* and then discovering it
        has no rows is the failure this avoids -- it would be reported as an
        unmeasurable model when the dataset simply has no rows of that one type.
        """
        key = (model._meta.label_lower, base, index)
        if key in self._generic_cache:
            return self._generic_cache[key]

        type_field = serializer.fields[f"{base}_type"]
        queryset, _ = self._related_queryset(f"{base}_type", type_field, model)
        for content_type in queryset.order_by("pk"):
            target = content_type.model_class()
            if target is None or target is model:
                continue
            rows = list(target._default_manager.order_by("pk")[index : index + 1])
            if rows:
                value = (f"{content_type.app_label}.{content_type.model}", str(rows[0].pk))
                self._generic_cache[key] = value
                return value
        raise Unbuildable(
            f"{base}_type",
            "no content type this field accepts has any rows"
            if index == 0
            else f"no content type this field accepts has {index + 1} rows",
        )

    # --- scalars ------------------------------------------------------------

    def _scalar_value(self, name, field, index=0, tag=""):
        # EmailField/SlugField/URLField are CharField subclasses, so the
        # specific ones are tested first.
        if isinstance(field, drf.EmailField):
            return f"{self.seed}{index}@example.invalid"
        if isinstance(field, drf.URLField):
            return f"https://example.invalid/{self.seed}{index}"
        if isinstance(field, drf.SlugField):
            return self._text(f"{self.seed}-{tag}-{index}".lower(), field)
        if isinstance(field, drf.IPAddressField):
            return "192.0.2.1"
        if isinstance(field, drf.CharField):
            return self._text(f"{self.seed}-{tag}-{index}", field)
        if isinstance(field, drf.BooleanField):
            return False
        if isinstance(field, drf.IntegerField):
            low = field.min_value if field.min_value is not None else 1
            high = field.max_value if field.max_value is not None else low
            return min(low, high)
        if isinstance(field, drf.DecimalField):
            return str(decimal.Decimal(1).quantize(decimal.Decimal(10) ** -field.decimal_places))
        if isinstance(field, drf.FloatField):
            return 1.0
        if isinstance(field, drf.DateTimeField):
            return "2026-01-01T00:00:00Z"
        if isinstance(field, drf.DateField):
            return "2026-01-01"
        if isinstance(field, drf.TimeField):
            return "00:00:00"
        if isinstance(field, drf.DurationField):
            return str(datetime.timedelta(seconds=1))
        if isinstance(field, (drf.JSONField, drf.DictField)):
            # Not `{}`: several models declare a JSON column `blank=False`, and an
            # empty object is blank as far as `full_clean` is concerned.
            return {self.seed: index}
        if isinstance(field, drf.ListField):
            child = getattr(field, "child", None)
            if child is None:
                return []
            return [self._scalar_value(name, child, index=index, tag=tag)]
        if isinstance(field, drf.UUIDField):
            raise Unbuildable(name, "bare UUID field with no queryset to draw from")
        raise Unbuildable(name, f"no generator for {type(field).__name__}")

    def _text(self, value, field):
        """Fit generated text to the field's declared length, without truncating to nothing."""
        max_length = getattr(field, "max_length", None)
        if max_length is not None and len(value) > max_length:
            if max_length < len(self.seed):
                raise Unbuildable(field.field_name, f"max_length={max_length} too short to generate into")
            value = value[:max_length]
        min_length = getattr(field, "min_length", None)
        if min_length is not None and len(value) < min_length:
            value = value.ljust(min_length, "x")
        return value

    @staticmethod
    def _first_choice(name, choices):
        """The first non-blank choice. Blank is skipped: a field that permits it
        usually treats it as 'unset', which is not what a populated field costs."""
        for key in choices:
            if key not in ("", None):
                return key
        raise Unbuildable(name, "choice field with no non-blank choices")

    # --- related objects ----------------------------------------------------

    def _model_field(self, model, field, name):
        """The concrete model field behind a serializer field, or None.

        ``source`` is what DRF resolves against the model, and it is not always
        the serializer field's name.
        """
        source = field.source or name
        if "." in source or source == "*":
            return None
        try:
            return model._meta.get_field(source)
        except (FieldDoesNotExist, AttributeError):
            return None

    def _is_unique_fk(self, model, field, name):
        model_field = self._model_field(model, field, name)
        return bool(model_field is not None and getattr(model_field, "one_to_one", False))

    def _related_queryset(self, name, field, model):
        """The queryset a related field's value must come from.

        ``limit_choices_to`` is the part that matters and the part a naive
        implementation misses. Nautobot's ``StatusField``/``RoleField`` derive it
        from the content type they are attached to, so ``Status.objects.first()``
        is a Status for some *other* model and the create fails validation --
        which would be recorded as an unmeasurable model when in fact the
        payload builder simply picked the wrong row.
        """
        queryset = getattr(field, "queryset", None)
        model_field = self._model_field(model, field, name)
        if queryset is None:
            if model_field is None or not getattr(model_field, "related_model", None):
                raise Unbuildable(name, "related field with neither queryset nor resolvable model field")
            queryset = model_field.related_model._default_manager.all()
        if model_field is not None:
            queryset = self._apply_limit(queryset, model_field.get_limit_choices_to())
        return queryset, model_field

    @staticmethod
    def _apply_limit(queryset, limit):
        """Apply a `limit_choices_to` of any of the three shapes Nautobot uses.

        This is the single most consequential line in the module. Nautobot's
        `ForeignKeyLimitedByContentTypes` returns a **dict** -- `{content_types__
        app_label: ..., content_types__model: ...}` -- and `queryset.filter(dict)`
        is a `FieldError`, not a filter. Getting it wrong meant every model with
        a status or a role failed to build a payload: 22 of 152, including
        device, interface, prefix, ipaddress, cable, location, circuit and rack,
        which is most of what a write matrix exists to measure.
        """
        if not limit:
            return queryset
        if callable(limit):  # Django resolves these, but a queryset can carry a raw one
            limit = limit()
        if isinstance(limit, dict):
            return queryset.filter(**limit)
        if isinstance(limit, Q):
            return queryset.filter(limit)
        if hasattr(limit, "get_query"):
            return queryset.filter(limit.get_query())
        return queryset

    def _related_value(self, name, field, model, unique=False, index=0):
        """``unique`` means "payload i must not reuse payload j's target".

        True for a one-to-one, and true for any field in a uniqueness constraint
        when building a bulk payload. A shared target is correct otherwise --
        fifty interfaces on one device is a realistic create, and forcing fifty
        different devices would measure a different thing.
        """
        queryset, model_field = self._related_queryset(name, field, model)
        cache_key = None if unique else (model._meta.label_lower, name)
        if cache_key is not None and cache_key in self._related_cache:
            return self._related_cache[cache_key]

        if unique and model_field is not None and getattr(model_field, "one_to_one", False):
            # An already-taken one-to-one target fails a uniqueness check rather
            # than a validation check, which would be recorded as an invalid
            # payload when the builder simply picked a used row.
            taken = model._default_manager.exclude(**{f"{model_field.name}__isnull": True}).values_list(
                f"{model_field.name}_id", flat=True
            )
            queryset = queryset.exclude(pk__in=taken)

        # A non-unique field always draws the first row (and is then cached), so
        # the offset only ever applies to the unique case.
        offset = index if unique else 0
        rows = list(queryset.order_by("pk")[offset : offset + 1])
        instance = rows[0] if rows else None
        if instance is None:
            label = queryset.model._meta.label_lower
            raise Unbuildable(
                name,
                f"fewer than {index + 1} distinct {label} rows to reference"
                if unique
                else f"no {label} rows available to reference",
            )
        value = str(instance.pk)
        if cache_key is not None:
            self._related_cache[cache_key] = value
        return value

    def _content_type_value(self, name, field, model):
        """ContentTypeField takes 'app_label.model', not a pk."""
        queryset, _ = self._related_queryset(name, field, model)
        instance = queryset.order_by("pk").first()
        if instance is None:
            raise Unbuildable(name, "no content types match this field's limit_choices_to")
        return f"{instance.app_label}.{instance.model}"

    # --- IPAM seeds ---------------------------------------------------------
    #
    # The one place this module needs to know something about the *data* rather
    # than the schema. An address is only valid inside a prefix that exists, in
    # the namespace that prefix belongs to, and only if nothing has taken it --
    # none of which is derivable from the serializer field, and all of which is
    # cheap to look up once.

    def _ipam_pool(self):
        """One real prefix with room, and its unallocated host addresses.

        Chosen deterministically (first by pk with enough space) and cached, so
        every IPAM payload in a run draws from the same prefix and two runs
        against the same dataset produce the same addresses.
        """
        if self._free_hosts is not None:
            return self._free_hosts
        from nautobot.ipam.models import IPAddress, Prefix  # deferred: app registry must be ready

        self._free_hosts = {}
        for prefix in Prefix.objects.order_by("pk").iterator():
            size = prefix.prefix.size
            if size < 64:
                continue
            taken = set(IPAddress.objects.filter(parent=prefix).values_list("host", flat=True))
            free = [str(prefix.prefix[i]) for i in range(1, min(size - 1, 512)) if str(prefix.prefix[i]) not in taken]
            if len(free) >= 64:  # enough for a bulk of ten address ranges
                self._free_hosts = {"prefix": prefix, "free": free}
                break
        return self._free_hosts

    def _free_host(self, offset):
        pool = self._ipam_pool()
        if not pool or offset >= len(pool["free"]):
            raise Unbuildable("address", "no prefix in this dataset has enough unallocated host addresses")
        return f"{pool['free'][offset]}/{pool['prefix'].prefix_length}"

    def _seed_address(self, model, index):
        return self._free_host(index)

    def _seed_namespace(self, model, index):
        pool = self._ipam_pool()
        if not pool:
            raise Unbuildable("namespace", "no usable prefix, so no namespace to write into")
        return str(pool["prefix"].namespace_id)

    def _seed_range_start(self, model, index):
        # A range endpoint is a bare host, not host/mask: the mask comes from the
        # namespace's prefix, and sending one is rejected as an invalid address.
        return self._free_host(index * 4).split("/")[0]

    def _seed_range_end(self, model, index):
        return self._free_host(index * 4 + 2).split("/")[0]

    def _seed_prefix(self, model, index):
        """A prefix that does not exist yet.

        198.51.0.0/16 rather than a subnet of something already in the dataset:
        creating a prefix inside an existing one reparents its children, which
        would make the measurement a tree-rebuild rather than a create -- a
        different and much more expensive operation wearing the same name.
        """
        return f"198.51.{100 + index}.0/24"


class _Omit:
    """Sentinel: this field should be left out of the payload entirely."""

    def __repr__(self):
        return "<omit>"


_OMIT = _Omit()
