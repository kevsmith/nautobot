from typing import ClassVar

from django.db.models import Count, OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce

from nautobot.core.models.utils import deconstruct_composite_key
from nautobot.core.utils import permissions
from nautobot.core.utils.data import merge_dicts_without_collision


def count_related(model, field, *, filter_dict=None, manager_name="objects", distinct=False):
    """
    Return a Subquery suitable for annotating a child object count.

    Args:
        model (Model): The related model to aggregate
        field (str): The field on the related model which points back to the OuterRef model
        filter_dict (dict): Optional dict of filter key/value pairs to limit the Subquery
        manager_name (str): Name of the manager on the model to use
    """
    filters = {field: OuterRef("pk")}
    if filter_dict:
        filters.update(filter_dict)

    manager = getattr(model, manager_name)
    if hasattr(manager, "without_tree_fields"):
        manager = manager.without_tree_fields()
    qs = manager.filter(**filters).order_by().values(field)
    if distinct:
        qs = qs.annotate(c=Count("pk", distinct=distinct)).values("c")
    else:
        qs = qs.annotate(c=Count("*")).values("c")
    subquery = Subquery(qs)

    return Coalesce(subquery, 0)


class CompositeKeyQuerySetMixin:
    """
    Mixin to extend a base queryset class with support for filtering by `composite_key=...` as a virtual parameter.

    Example:

        >>> Location.objects.last().composite_key
        'Durham;AMER'

    Note that `Location.composite_key` is a `@property`, *not* a database field, and so would not normally be usable in
    a `QuerySet` query, but because `RestrictedQuerySet` inherits from this mixin, the following "just works":

        >>> Location.objects.get(composite_key="Durham;AMER")
        <Location: Durham>

    This is a shorthand for what would otherwise be a multi-step process:

        >>> from nautobot.core.models.utils import deconstruct_composite_key
        >>> deconstruct_composite_key("Durham;AMER")
        ['Durham', 'AMER']
        >>> Location.natural_key_args_to_kwargs(['Durham', 'AMER'])
        {'name': 'Durham', 'parent__name': 'AMER'}
        >>> Location.objects.get(name="Durham", parent__name="AMER")
        <Location: Durham>

    This works for QuerySet `filter()` and `exclude()` as well:

        >>> Location.objects.filter(composite_key='Durham;AMER')
        <LocationQuerySet [<Location: Durham>]>
        >>> Location.objects.exclude(composite_key='Durham;AMER')
        <LocationQuerySet [<Location: AMER>]>

    `composite_key` can also be used in combination with other query parameters:

        >>> Location.objects.filter(composite_key='Durham;AMER', status__name='Planned')
        <LocationQuerySet []>

    It will raise a ValueError if the deconstructed composite key collides with another query parameter:

        >>> Location.objects.filter(composite_key='Durham;AMER', name='Raleigh')
        ValueError: Conflicting values for key "name": ('Durham', 'Raleigh')

    See also `BaseModel.composite_key` and `utils.construct_composite_key()`/`utils.deconstruct_composite_key()`.
    """

    def split_composite_key_into_kwargs(self, composite_key=None, **kwargs):
        """
        Helper method abstracting a common need from filter() and exclude().

        Subclasses may need to call this directly if they also have special processing of other filter/exclude params.
        """
        if composite_key and isinstance(composite_key, str):
            natural_key_values = deconstruct_composite_key(composite_key)
            return merge_dicts_without_collision(self.model.natural_key_args_to_kwargs(natural_key_values), kwargs)
        return kwargs

    def filter(self, *args, composite_key=None, **kwargs):
        """
        Explicitly handle `filter(composite_key="...")` by decomposing the composite-key into natural key parameters.

        Counterpart to BaseModel.composite_key property.
        """
        return super().filter(*args, **self.split_composite_key_into_kwargs(composite_key, **kwargs))

    def exclude(self, *args, composite_key=None, **kwargs):
        """
        Explicitly handle `exclude(composite_key="...")` by decomposing the composite-key into natural key parameters.

        Counterpart to BaseModel.composite_key property.
        """
        return super().exclude(*args, **self.split_composite_key_into_kwargs(composite_key, **kwargs))


class RestrictedQuerySet(CompositeKeyQuerySetMixin, QuerySet):
    def _fetch_all(self):
        """Return no rows without asking the database, for a queryset that is empty by construction.

        `.none()` marks the query empty, but evaluating it still builds and compiles SQL and
        then discards it at `EmptyResultSet`. Measured at 1033us for Device against 12us for
        the `query.is_empty()` check that can answer instead.

        That path is reached constantly rather than rarely. Django's
        `ModelMultipleChoiceField.clean()` returns `self.queryset.none()` for every filter left
        blank, and both django-filter (`if not value`) and `nautobot.core.filters` (`value.exists()`)
        then truth-test the result. A device list document request compiled 99 such queries for
        42.7ms of a 253ms response, its row request 78 for 31.1ms, and a VLAN list 39 for 13.9ms.
        `restrict()` returning `self.none()` for an unpermitted user reaches the same path.

        Assigning `_result_cache` and deferring to the parent keeps Django's own semantics: the
        parent skips its `list(self._iterable_class(self))` because the cache is populated, and
        still runs the prefetch step.
        """
        if self._result_cache is None and self.query.is_empty():
            self._result_cache = []
        super()._fetch_all()

    def exists(self):
        """Answer `False` without asking the database, for a queryset that is empty by construction.

        `_fetch_all` covers `__bool__`, `__iter__`, `__len__` and `list()`; `exists()` takes its
        own route through `query.has_results()`, which compiles and discards in the same way.
        `nautobot.core.filters.TreeNodeMultipleChoiceFilter.filter` calls it on a cleaned empty
        value, which is 6 of a device list's 78 discarded compilations.
        """
        if self._result_cache is None and self.query.is_empty():
            return False
        return super().exists()

    def iterator(self, *args, **kwargs):
        """Yield nothing without asking the database, for a queryset that is empty by construction.

        `iterator()` deliberately bypasses `_result_cache` and therefore `_fetch_all`, so the guard
        there cannot see it. Django's `ModelChoiceIterator.__iter__` takes exactly this route
        (`queryset = queryset.iterator()` when there are no prefetch lookups), which is why an
        `APISelect` rendering no options still compiled a query per widget: 19 of a device list's
        remaining 20 discarded compilations after the `_fetch_all` guard landed, measured rather
        than assumed.
        """
        if self.query.is_empty():
            return iter([])
        return super().iterator(*args, **kwargs)

    def count(self):
        """Answer `0` without asking the database, for a queryset that is empty by construction.

        `count()` compiles an aggregate rather than a select, so it too misses the `_fetch_all`
        guard. A paginated list view counts its queryset once per request, which is the single
        remaining discarded compilation at `core/views/renderers.py:136` once the other three
        guards are in place.
        """
        if self._result_cache is None and self.query.is_empty():
            return 0
        return super().count()

    def restrict(self, user, action="view"):
        """
        Filter the QuerySet to return only objects on which the specified user has been granted the specified
        permission.

        Args:
            user (User): User instance
            action (str): The action which must be permitted (e.g. "view" for "dcim.view_location"); default is 'view'
        """
        # Resolve the full name of the required permission
        app_label = self.model._meta.app_label
        model_name = self.model._meta.model_name
        permission_required = f"{app_label}.{action}_{model_name}"

        # Bypass restriction for superusers and exempt views
        if user.is_superuser or permissions.permission_is_exempt(permission_required):
            # This is a cache buster to ensure that we always return a new QuerySet
            qs = self.all()

        # User is anonymous or has not been granted the requisite permission
        elif not user.is_authenticated or permission_required not in user.get_all_permissions():
            qs = self.none()

        # Filter the queryset to include only objects with allowed attributes
        else:
            tokens = {
                "$user": user,
            }

            attrs = permissions.qs_filter_from_constraints(user._object_perm_cache[permission_required], tokens)
            if attrs:
                # Use a subquery to avoid duplicate results when constraints span many-to-many joins
                # (e.g. tags__name__regex matching multiple tags on the same object).
                # See: https://github.com/nautobot/nautobot/issues/8690
                inner_qs = self.model._default_manager.filter(attrs)
                if hasattr(inner_qs, "without_tree_fields"):
                    inner_qs.without_tree_fields()
                qs = self.filter(pk__in=inner_qs.values("pk"))
            else:
                qs = self.all()

        return qs

    def check_perms(self, user, *, instance=None, pk=None, action="view"):
        """
        Check whether the given user can perform the given action with regard to the given instance of this model.

        Either instance or pk must be specified, but not both.

        Args:
          user (User): User instance
          instance (self.model): Instance of this queryset's model to check, if pk is not provided
          pk (uuid): Primary key of the desired instance to check for, if instance is not provided
          action (str): The action which must be permitted (e.g. "view" for "dcim.view_location"); default is 'view'

        Returns:
            (bool): Whether the action is permitted or not
        """
        if instance is not None and pk is not None and instance.pk != pk:
            raise RuntimeError("Should not be called with both instance and pk specified!")
        if instance is None and pk is None:
            raise ValueError("Either instance or pk must be specified!")
        if instance is not None and not isinstance(instance, self.model):
            raise TypeError(f"{instance} is not a {self.model}")
        if pk is None:
            pk = instance.pk

        return self.restrict(user, action).filter(pk=pk).exists()

    def distinct_values_list(self, *fields, flat=False, named=False):
        """Wrapper for `QuerySet.values_list()` that adds the `distinct()` query to return a list of unique values.

        Note:
            Uses `QuerySet.order_by()` to disable ordering, preventing unexpected behavior when using `values_list` described
            in the Django `distinct()` documentation at https://docs.djangoproject.com/en/stable/ref/models/querysets/#distinct

        Args:
            *fields (str): Optional positional arguments which specify field names.
            flat (bool): Set to True to return a QuerySet of individual values instead of a QuerySet of tuples.
                Defaults to False.
            named (bool): Set to True to return a QuerySet of namedtuples. Defaults to False.

        Returns:
            (QuerySet): A QuerySet of tuples or, if `flat` is set to True, a queryset of individual values.

        """
        return self.order_by().values_list(*fields, flat=flat, named=named).distinct()


class BaseManyToManyQuerySetMixin:
    """
    Base mixin to provide backward compatibility for fields that have been changed from ForeignKey to ManyToManyField.

    Subclasses should define FIELD_MAP as a dictionary of field mappings, where the key is the old field name
    and the value is the new field name.
    """

    FIELD_MAP: ClassVar[dict[str, str]] = {}

    def __init_subclass__(cls, **kwargs):
        """Combine FIELD_MAP from all parent classes into a single dictionary."""
        super().__init_subclass__(**kwargs)
        combined_field_map = {}
        for base in reversed(cls.__mro__):
            if hasattr(base, "FIELD_MAP") and isinstance(getattr(base, "FIELD_MAP"), dict):
                combined_field_map.update(base.FIELD_MAP)
        cls.FIELD_MAP = combined_field_map

    def _convert_to_m2m_field(self, kwargs):
        field_mappings = self.FIELD_MAP
        if not field_mappings:
            return kwargs

        updated_kwargs = {}

        for field, value in kwargs.items():
            converted = False

            # Check each field mapping
            for old_field, new_field in field_mappings.items():
                if field == old_field:
                    # Direct field query becomes __in for ManyToMany
                    updated_kwargs[f"{new_field}__in"] = [value]
                    converted = True
                    break
                elif field.startswith(f"{old_field}__"):
                    # Replace old field prefix with new field prefix
                    updated_kwargs[field.replace(old_field, new_field, 1)] = value
                    converted = True
                    break

            if not converted:
                updated_kwargs[field] = value

        return updated_kwargs

    def filter(self, *args, **kwargs):
        kwargs = self._convert_to_m2m_field(kwargs)
        return super().filter(*args, **kwargs)

    def exclude(self, *args, **kwargs):
        kwargs = self._convert_to_m2m_field(kwargs)
        return super().exclude(*args, **kwargs)


class LocationToLocationsQuerySetMixin(BaseManyToManyQuerySetMixin):
    """
    Mixin to convert 'location' to 'locations' in queryset parameters.
    """

    FIELD_MAP = {"location": "locations"}


class ClusterToClustersQuerySetMixin(BaseManyToManyQuerySetMixin):
    """
    Mixin to convert 'cluster' to 'clusters' in queryset parameters.
    """

    FIELD_MAP = {"cluster": "clusters"}
