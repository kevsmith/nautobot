import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.urls import NoReverseMatch, reverse
from django.utils.encoding import is_protected_type
from django.utils.functional import classproperty

from nautobot.core.models.managers import BaseManager
from nautobot.core.models.querysets import (
    ClusterToClustersQuerySetMixin,
    CompositeKeyQuerySetMixin,
    LocationToLocationsQuerySetMixin,
    RestrictedQuerySet,
)
from nautobot.core.models.utils import (
    cached_natural_key_field_lookups,
    construct_composite_key,
    construct_natural_slug,
    deconstruct_composite_key,
)
from nautobot.core.utils.cache import construct_cache_key, get_request_cache
from nautobot.core.utils.lookup import get_route_for_model

__all__ = (
    "BaseManager",
    "BaseModel",
    "ClusterToClustersQuerySetMixin",
    "CompositeKeyQuerySetMixin",
    "ContentTypeRelatedQuerySet",
    "LocationToLocationsQuerySetMixin",
    "RestrictedQuerySet",
    "construct_composite_key",
    "construct_natural_slug",
    "deconstruct_composite_key",
)


_UNSET = object()

#: Upper bound on the number of rows a model may have and still participate in the natural-key map cache
#: (see `BaseModel.natural_key_map`). The map is held whole in memory and in a single cache entry, so this
#: exists to keep an unexpectedly large table from turning a small reference cache into a large one.
NATURAL_KEY_MAP_MAX_ROWS = 5000

#: Memo of `(model, lookup) -> (fk_attname, related_model)` for FK hops that can be resolved from a
#: natural-key map, or `None` for hops that cannot. Purely derived from model metadata and the static
#: `natural_key_map_enabled` flag, so it is safe to keep for the life of the process.
_natural_key_map_plans = {}


def _natural_key_map_plan(model, lookup):
    """Return `(fk_attname, related_model)` if `model.<lookup>` is an FK into a natural-key-mapped model."""
    key = (model, lookup)
    try:
        return _natural_key_map_plans[key]
    except KeyError:
        pass
    plan = None
    try:
        field = model._meta.get_field(lookup)
    except FieldDoesNotExist:
        field = None
    if field is not None and field.is_relation and field.many_to_one and field.concrete:
        if getattr(field.related_model, "natural_key_map_enabled", False):
            plan = (field.attname, field.related_model)
    _natural_key_map_plans[key] = plan
    return plan


class BaseModel(models.Model):
    """
    Base model class that all models should inherit from.

    This abstract base provides globally common fields and functionality.

    Here we define the primary key to be a UUID field and set its default to
    automatically generate a random UUID value. Note however, this does not
    operate in the same way as a traditional auto incrementing field for which
    the value is issued by the database upon initial insert. In the case of
    the UUID field, Django creates the value upon object instantiation. This
    means the canonical pattern in Django of checking `self.pk is None` to tell
    if an object has been created in the actual database does not work because
    the object will always have the value populated prior to being saved to the
    database for the first time. An alternate pattern of checking `not self.present_in_database`
    can be used for the same purpose in most cases.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, unique=True, editable=False)

    objects = BaseManager.from_queryset(RestrictedQuerySet)()
    #: Set to True on small reference models (see `BaseModel.natural_key_map`) whose natural keys are
    #: repeatedly resolved as part of *other* models' natural keys.
    natural_key_map_enabled = False
    is_contact_associable_model = False  # ContactMixin overrides this to default True
    is_dynamic_group_associable_model = False  # DynamicGroupMixin overrides this to default True
    is_metadata_associable_model = True
    is_saved_view_model = False  # SavedViewMixin overrides this to default True
    is_cloud_resource_type_model = False  # CloudResourceTypeMixin overrides this to default True
    is_approval_workflow_model = False  # ApprovableModelMixin overrides this to default True

    associated_object_metadata = GenericRelation(
        "extras.ObjectMetadata",
        content_type_field="assigned_object_type",
        object_id_field="assigned_object_id",
        related_query_name="associated_object_metadata_%(app_label)s_%(class)s",  # e.g. 'associated_object_metadata_dcim_device'
    )
    associated_data_compliance = GenericRelation(
        "data_validation.DataCompliance",
        content_type_field="content_type",
        object_id_field="object_id",
        related_query_name="associated_data_compliance_%(app_label)s_%(class)s",  # e.g. 'associated_data_compliance_dcim_device'
    )

    class Meta:
        abstract = True

    def get_absolute_url(self, api=False):
        """
        Return the canonical URL for this object in either the UI or the REST API.
        """

        # Iterate the pk-like fields and try to get a URL, or return None.
        fields = ["pk"]
        actions = ["retrieve", "detail", ""]  # TODO: Eventually all retrieve

        for field in fields:
            if not hasattr(self, field):
                continue

            for action in actions:
                route = get_route_for_model(self, action, api=api)

                try:
                    return reverse(route, kwargs={field: getattr(self, field)})
                except NoReverseMatch:
                    continue

        raise AttributeError(f"Cannot find a URL for {self} ({self._meta.app_label}.{self._meta.model_name})")

    @property
    def page_title(self):
        """
        Property used by Title and Breadcrumbs to display link to the object or title at detail page.
        """
        if hasattr(self, "name"):
            return self.name
        if hasattr(self, "display"):
            return self.display
        return str(self)

    @property
    def present_in_database(self):
        """
        True if the record exists in the database, False if it does not.
        """
        return not self._state.adding

    @classproperty  # https://github.com/PyCQA/pylint-django/issues/240
    def _content_type(cls):  # pylint: disable=no-self-argument
        """
        Return the ContentType of the object, never cached.
        """
        return ContentType.objects.get_for_model(cls)

    @classproperty  # https://github.com/PyCQA/pylint-django/issues/240
    def _content_type_cache_key(cls):  # pylint: disable=no-self-argument
        """
        Return the cache key for the ContentType of the object.

        Necessary for use with _content_type_cached and management commands.
        """
        return construct_cache_key(cls, method_name="_content_type")

    @classproperty  # https://github.com/PyCQA/pylint-django/issues/240
    def _content_type_cached(cls):  # pylint: disable=no-self-argument
        """
        Return the ContentType of the object, cached.
        """

        return cache.get_or_set(cls._content_type_cache_key, cls._content_type, settings.CONTENT_TYPE_CACHE_TIMEOUT)

    def validated_save(self, *args, **kwargs):
        """
        Perform model validation during instance save.

        This is a convenience method that first calls `self.full_clean()` and then `self.save()`
        which in effect enforces model validation prior to saving the instance, without having
        to manually make these calls seperately. This is a slight departure from Django norms,
        but is intended to offer an optional, simplified interface for performing this common
        workflow. The intended use is for user defined Jobs run via the `nautobot-server nbshell`
        command.
        """
        self.full_clean()
        self.save(*args, **kwargs)

    validated_save.alters_data = True

    @classmethod
    def _build_natural_key_map(cls, lookups):
        """Load `{pk: natural key values}` for every instance of this model in a single values-only query."""
        queryset = cls.objects.all()
        # Tree models default to fetching tree fields, which we neither need nor want to pay a recursive CTE for.
        without_tree_fields = getattr(queryset, "without_tree_fields", None)
        if without_tree_fields is not None:
            queryset = without_tree_fields()
        # `values_list` rather than model instances on purpose: this is one flat row per object, no model
        # instantiation, and the joins it does are over a small reference table.
        rows = list(queryset.values_list("pk", *lookups)[: NATURAL_KEY_MAP_MAX_ROWS + 1])
        if len(rows) > NATURAL_KEY_MAP_MAX_ROWS:
            # Not a small reference table after all. Cache the fact so we don't re-check on every request;
            # `natural_key()` falls back to per-object attribute traversal.
            return False
        return {
            row[0]: tuple(value if is_protected_type(value) else str(value) for value in row[1:])
            for row in rows
        }

    @classmethod
    def natural_key_map(cls):
        """
        `{pk: natural key values}` for all instances of this model, or `None` if this model doesn't provide one.

        Only models that opt in via `natural_key_map_enabled` provide a map. It exists for small reference
        models -- Location above all -- whose natural keys are embedded in *other* models' natural key field
        lookups: serializing one page of Interfaces otherwise walks `device -> location -> parent -> parent...`
        with an uncached attribute access, and therefore a query, at every hop of every object.

        The map is built at most once per `request_cache()` scope -- a single values-only query for the whole
        table, rather than a cache read per object -- and is discarded when that scope exits. It therefore
        cannot be stale relative to anything else the same request read, and needs no invalidation. Outside
        such a scope (a management command, a test) this returns `None` and `natural_key()` traverses
        attributes exactly as it did before.

        Values are returned unstripped -- one entry per lookup in `natural_key_field_lookups`, including
        trailing `None`s -- so that a caller resolving a single lookup can index into the tuple directly.
        """
        if not cls.natural_key_map_enabled:
            return None
        request_local_cache = get_request_cache()
        if request_local_cache is None:
            return None
        lookups = cls.natural_key_field_lookups
        cache_key = construct_cache_key(
            cls.objects, method_name="natural_key_map", branch_aware=False, lookups=",".join(lookups)
        )
        if cache_key not in request_local_cache:
            request_local_cache[cache_key] = cls._build_natural_key_map(lookups)
        return request_local_cache[cache_key] or None  # `False` is the "too many rows to cache" sentinel

    def natural_key(self) -> list:
        """
        Smarter default implementation of natural key construction.

        1. Handles nullable foreign keys (https://github.com/wq/django-natural-keys/issues/18)
        2. Handles variadic natural-keys (e.g. Location model - [name, parent__name, parent__parent__name, ...].)
        3. Resolves foreign-key hops into small reference models from `natural_key_map()` rather than by
           fetching the related object, when the related object isn't already loaded in memory.
        """
        vals = []
        for lookups in [lookup.split("__") for lookup in self.natural_key_field_lookups]:
            val = self
            last_index = len(lookups) - 1
            for index, lookup in enumerate(lookups):
                if index < last_index:
                    plan = _natural_key_map_plan(type(val), lookup)
                    # If the related object is already loaded (e.g. via select_related), keep using it: it is
                    # free to traverse, and it -- not the database -- is what the caller expects to be read.
                    if plan is not None and lookup not in val._state.fields_cache:
                        fk_attname, related_model = plan
                        related_pk = getattr(val, fk_attname)
                        if related_pk is None:
                            val = None
                            break
                        natural_key_map = related_model.natural_key_map()
                        if natural_key_map is not None:
                            row = natural_key_map.get(related_pk)
                            related_lookups = related_model.natural_key_field_lookups
                            remaining_lookup = "__".join(lookups[index + 1 :])
                            if row is not None and remaining_lookup in related_lookups:
                                val = row[related_lookups.index(remaining_lookup)]
                                break
                        # Anything else (stale map, dangling FK, a lookup that isn't part of the related
                        # model's own natural key) falls through to the general traversal below.
                val = getattr(val, lookup)
                if val is None:
                    break
            if not is_protected_type(val):
                val = str(val)
            vals.append(val)
        # Strip trailing Nones from vals
        while vals and vals[-1] is None:
            vals.pop()
        return vals

    @property
    def composite_key(self) -> str:
        """
        Automatic "slug" string derived from this model's natural key, suitable for use in URLs etc.

        A less naïve implementation than django-natural-keys provides by default, based around URL percent-encoding.
        """
        return construct_composite_key(self.natural_key())

    @property
    def natural_slug(self) -> str:
        """
        Automatic "slug" string derived from this model's natural key. This differs from composite
        key in that it must be human-readable and comply with a very limited character set, and is therefore lossy.
        This value is not guaranteed to be
        unique although a best effort is made by appending a fragment of the primary key to the
        natural slug value.
        """
        return construct_natural_slug(self.natural_key(), pk=self.pk)

    @classmethod
    def _generate_field_lookups_from_natural_key_field_names(cls, natural_key_field_names):
        """Generate field lookups based on natural key field names."""
        natural_key_field_lookups = []
        for field_name in natural_key_field_names:
            # field_name could be a related field that has its own natural key fields (`parent`),
            # *or* it could be an explicit set of traversals (`parent__namespace__name`). Handle both.
            model = cls
            for field_component in field_name.split("__")[:-1]:
                model = model._meta.get_field(field_component).remote_field.model

            try:
                field = model._meta.get_field(field_name.split("__")[-1])
            except FieldDoesNotExist:
                # Not a database field, maybe it's a property instead?
                if hasattr(model, field_name) and isinstance(getattr(model, field_name), property):
                    natural_key_field_lookups.append(field_name)
                    continue
                raise

            if getattr(field, "remote_field", None) is None:
                # Not a related field, so the field name is the field lookup
                natural_key_field_lookups.append(field_name)
                continue

            related_model = field.remote_field.model
            related_natural_key_field_lookups = None
            if hasattr(related_model, "natural_key_field_lookups"):
                # TODO: generic handling for self-referential case, as seen in Location
                related_natural_key_field_lookups = related_model.natural_key_field_lookups
            else:
                # Related model isn't a Nautobot model and so doesn't have a `natural_key_field_lookups`.
                # The common case we've encountered so far is the contenttypes.ContentType model:
                if related_model._meta.app_label == "contenttypes" and related_model._meta.model_name == "contenttype":
                    related_natural_key_field_lookups = ["app_label", "model"]
                # Additional special cases can be added here

            if not related_natural_key_field_lookups:
                raise AttributeError(
                    f"Unable to determine the related natural-key fields for {related_model.__name__} "
                    f"(as referenced from {cls.__name__}.{field_name}). If the related model is a non-Nautobot "
                    "model (such as ContentType) then it may be appropriate to add special-case handling for this "
                    "model in BaseModel.natural_key_field_lookups; alternately you may be able to solve this for "
                    f"a single special case by explicitly defining {cls.__name__}.natural_key_field_lookups."
                )

            for field_lookup in related_natural_key_field_lookups:
                natural_key_field_lookups.append(f"{field_name}__{field_lookup}")

        return natural_key_field_lookups

    @classmethod
    def csv_natural_key_field_lookups(cls):
        """Override this method for models with Python `@property` as part of their `natural_key_field_names`.

        Since CSV export for `natural_key_field_names` relies on database fields, you can override this method
        to provide custom handling for models with property-based natural keys.
        """
        return cls.natural_key_field_lookups

    @classproperty  # https://github.com/PyCQA/pylint-django/issues/240
    @cached_natural_key_field_lookups
    def natural_key_field_lookups(cls):  # pylint: disable=no-self-argument
        """
        List of lookups (possibly including nested lookups for related models) that make up this model's natural key.

        BaseModel provides a "smart" implementation that tries to determine this automatically,
        but you can also explicitly set `natural_key_field_names` on a given model subclass if desired.

        This property is based on a consolidation of `django-natural-keys` `ForeignKeyModel.get_natural_key_info()`,
        `ForeignKeyModel.get_natural_key_def()`, and `ForeignKeyModel.get_natural_key_fields()`.

        Unlike `get_natural_key_def()`, this doesn't auto-exclude all AutoField and BigAutoField fields,
        but instead explicitly discounts the `id` field (only) as a candidate.
        """
        if cls != cls._meta.concrete_model:
            return cls._meta.concrete_model.natural_key_field_lookups
        # First, figure out which local fields comprise the natural key.
        # Fetch once rather than hasattr() + attribute access: `natural_key_field_names` is itself a
        # classproperty on several models, so testing and then reading it evaluates it twice.
        natural_key_field_names = getattr(cls, "natural_key_field_names", _UNSET)
        if natural_key_field_names is _UNSET:
            natural_key_field_names = []
            # Does this model have any new-style UniqueConstraints? If so, pick the first one
            for constraint in cls._meta.constraints:
                if isinstance(constraint, models.UniqueConstraint):
                    natural_key_field_names = constraint.fields
                    break
            else:
                # Else, does this model have any old-style unique_together? If so, pick the first one.
                if cls._meta.unique_together:
                    natural_key_field_names = cls._meta.unique_together[0]
                else:
                    # Else, do we have any individual unique=True fields? If so, pick the first one.
                    unique_fields = [field for field in cls._meta.fields if field.unique and field.name != "id"]
                    if unique_fields:
                        natural_key_field_names = (unique_fields[0].name,)

        if not natural_key_field_names:
            raise AttributeError(
                f"Unable to identify an intrinsic natural-key definition for {cls.__name__}. "  # pylint: disable=no-member
                "If there isn't at least one UniqueConstraint, unique_together, or field with unique=True, "
                "you probably need to explicitly declare the 'natural_key_field_names' for this model, "
                "or potentially override the default 'natural_key_field_lookups' implementation for this model."
            )

        # Next, for any natural key fields that have related models, get the natural key for the related model if known
        return cls._generate_field_lookups_from_natural_key_field_names(natural_key_field_names)

    @classmethod
    def natural_key_args_to_kwargs(cls, args):
        """
        Helper function to map a list of natural key field values to actual kwargs suitable for lookup and filtering.

        Based on `django-natural-keys` `NaturalKeyQuerySet.natural_key_kwargs()` method.
        """
        args = list(args)
        natural_key_field_lookups = cls.natural_key_field_lookups
        # Because `natural_key` strips trailing `None` from the natural key to handle the variadic-natural-key case,
        # we may need to add trailing `None` back on to make the number of args match back up.
        while len(args) < len(natural_key_field_lookups):
            args.append(None)
        # However, if we have *too many* args, that's just incorrect usage:
        if len(args) > len(natural_key_field_lookups):
            raise ValueError(
                f"Wrong number of natural-key args for {cls.__name__}.natural_key_args_to_kwargs() -- "
                f"expected no more than {len(natural_key_field_lookups)} but got {len(args)}."
            )
        return dict(zip(natural_key_field_lookups, args))


class ContentTypeRelatedQuerySet(RestrictedQuerySet):
    def get_for_model(self, model):
        """
        Return all `self.model` instances assigned to the given model.
        """
        content_type = ContentType.objects.get_for_model(model._meta.concrete_model)
        return self.filter(content_types=content_type)

    # TODO(timizuo): Merge into get_for_model; Cant do this now cause it would require alot
    #  of refactoring
    def get_for_models(self, models_):
        """
        Return all `self.model` instances assigned to the given `_models`.
        """
        q = models.Q()
        for model in models_:
            q |= models.Q(app_label=model._meta.app_label, model=model._meta.model_name)
        content_types = ContentType.objects.filter(q)
        return self.filter(content_types__in=content_types)
