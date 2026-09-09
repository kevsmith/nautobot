import time
from unittest import skip
from unittest.mock import patch
import uuid

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Q, QuerySet as DjangoQuerySet
from django.db.models.sql.compiler import SQLCompiler
from django.test import override_settings, SimpleTestCase, tag
from django.test.utils import isolate_apps

from nautobot.core.models import utils as model_utils
from nautobot.core.models.utils import (
    cache_natural_key_field_lookups,
    construct_composite_key,
    construct_natural_slug,
    deconstruct_composite_key,
    m2m_through_data_fields,
)
from nautobot.core.testing import TestCase
from nautobot.dcim.models import (
    Cable,
    ControllerManagedDeviceGroup,
    Device,
    DeviceType,
    Location,
    LocationType,
    Manufacturer,
)
from nautobot.extras.models import SecretsGroup, Status, Tag
from nautobot.extras.utils import get_explicit_m2m_through_side_field_names
from nautobot.ipam.models import VLAN, VRF

User = get_user_model()


class ModelUtilsTestCase(TestCase):
    @skip("Composite keys aren't being supported at this time")
    def test_construct_deconstruct_composite_key(self):
        """Test that construct_composite_key() and deconstruct_composite_key() work and are symmetric."""
        for values, expected_composite_key in (
            (["alpha"], "alpha"),  # simplest case
            (["alpha", "beta"], "alpha;beta"),  # multiple inputs
            (["10.1.1.1/24", "fe80::1"], "10.1.1.1%2F24;fe80::1"),  # URL-safe ASCII characters, / is *not* path safe
            ([None, "Hello", None], "%00;Hello;%00"),  # Null values
            (["💩", "Everyone's favorite!"], "%F0%9F%92%A9;Everyone%27s+favorite%21"),  # Emojis and unsafe ASCII
        ):
            with self.subTest(values=values):
                composite_key = construct_composite_key(values)
                self.assertEqual(composite_key, expected_composite_key)
                self.assertEqual(deconstruct_composite_key(composite_key), values)

    def test_construct_natural_slug(self):
        """Test that `construct_natural_slug()` works as expected."""
        pk = uuid.uuid4()
        pk4 = str(pk)[:4]
        for values, expected_natural_slug in (
            (["Alpha"], "alpha"),  # simplest case
            (["alpha", "beta"], "alpha_beta"),  # multiple inputs
            (["Über Ålpha"], "uber-alpha"),  # accents/ligatures
            (["10.1.1.1/24", "fe80::1"], "10-1-1-1-24_fe80-1"),  # URL-safe ASCII characters, / is *not* path safe
            ([None, "Hello", None], "_hello_"),  # Null values
            (["💩", "Everyone's favorite!"], "pile-of-poo_everyone-s-favorite"),  # Emojis and unsafe ASCII
        ):
            with self.subTest(values=values):
                expected_natural_slug += f"_{pk4}"
                natural_slug = construct_natural_slug(values, pk=pk)
                self.assertEqual(natural_slug, expected_natural_slug)

    def test_validated_save_calls_full_clean(self):
        with patch.object(Manufacturer, "clean", side_effect=ValidationError("clean was called")):
            with self.assertRaisesRegex(ValidationError, "clean was called"):
                Manufacturer(name="Cisco").validated_save()


@tag("unit")
class M2MThroughDataFieldsTestCase(SimpleTestCase):
    """`m2m_through_data_fields` separates a join table from one that records data about each pairing."""

    def _fields(self, model, field_name):
        return m2m_through_data_fields(model._meta.get_field(field_name).remote_field.through)

    def test_auto_created_through_is_a_pure_join(self):
        """Django's own through table holds nothing but the pk and the two foreign keys."""
        self.assertEqual(self._fields(VRF, "import_targets"), [])

    def test_custom_through_carrying_only_the_foreign_keys(self):
        """An explicit through model is not automatically data-carrying; `VLANLocationAssignment` just joins."""
        self.assertEqual(self._fields(VLAN, "locations"), [])

    def test_custom_through_carrying_data(self):
        self.assertEqual(self._fields(SecretsGroup, "secrets"), ["access_type", "secret_type"])

    def test_foreign_key_that_is_not_a_side_is_data(self):
        """`ControllerManagedDeviceGroupWirelessNetworkAssignment.vlan` is data about the pairing, not a side of it."""
        self.assertEqual(self._fields(ControllerManagedDeviceGroup, "wireless_networks"), ["vlan"])

    def test_one_to_one_sides_belong_to_the_join(self):
        """`CableToCableTermination` reaches each termination through a `OneToOneField`; only its own columns are data."""
        self.assertEqual(self._fields(Cable, "interfaces"), ["cable_end", "connector"])

    def test_side_of_one_relation_is_not_data_on_another(self):
        """`VRFDeviceAssignment` serves three relations, and every side of any of them is part of the join."""
        for field_name in ("devices", "virtual_machines", "virtual_device_contexts"):
            with self.subTest(field_name=field_name):
                self.assertEqual(self._fields(VRF, field_name), ["name", "rd"])

    def test_generic_foreign_key_columns_belong_to_the_join(self):
        """`TaggedItem.object_id` identifies the member, so `tags` is a join rather than data-carrying."""
        self.assertEqual(self._fields(Device, "tags"), [])

    def test_bookkeeping_columns_are_not_data(self):
        """No through model reports its pk or its `created`/`last_updated` as data about the pairing."""
        for through in self._all_through_models():
            with self.subTest(through=through._meta.label):
                self.assertNotIn(through._meta.pk.name, m2m_through_data_fields(through))
                self.assertFalse({"created", "last_updated"} & set(m2m_through_data_fields(through)))

    def test_every_through_model_classifies(self):
        """A meta-test: whatever is added later must still be classifiable, and only by real columns."""
        side_field_names = get_explicit_m2m_through_side_field_names()
        for through in self._all_through_models():
            with self.subTest(through=through._meta.label):
                data_fields = m2m_through_data_fields(through)
                for field_name in data_fields:
                    field = through._meta.get_field(field_name)
                    self.assertTrue(field.concrete, f"{field_name} is not a database column")
                    self.assertTrue(field.editable, f"{field_name} is not user-editable")
                    self.assertNotIn(
                        field_name,
                        side_field_names.get(through, ()),
                        f"{field_name} is a side of the relation, not data about it",
                    )

    @staticmethod
    def _all_through_models():
        return {m2m_field.remote_field.through for model in apps.get_models() for m2m_field in model._meta.many_to_many}


class NaturalKeyTestCase(TestCase):
    """Test the various natural-key APIs for a few representative models."""

    def test_natural_key(self):
        """Test the natural_key() default implementation with some representative models."""
        # Simple case - single unique field becomes the natural key
        mfr = Manufacturer.objects.first()
        self.assertEqual(mfr.natural_key(), [mfr.name])
        # Derived case - unique_together plus a nested lookup
        dt = DeviceType.objects.first()
        self.assertEqual(dt.natural_key(), [dt.manufacturer.name, dt.model])

    @isolate_apps("nautobot.core.tests")
    def test_natural_key_with_proxy_model(self):
        """Test that natural_key_field_lookups function returns the same value as its base class."""

        class ProxyManufacturer(Manufacturer):
            class Meta:
                proxy = True

        self.assertEqual(ProxyManufacturer.natural_key_field_lookups, Manufacturer.natural_key_field_lookups)

    @skip("Composite keys aren't being supported at this time")
    def test_composite_key(self):
        """Test the composite_key default implementation with some representative models."""
        mfr = Manufacturer.objects.first()
        self.assertEqual(mfr.composite_key, construct_composite_key(mfr.natural_key()))
        dt = DeviceType.objects.first()
        self.assertEqual(dt.composite_key, construct_composite_key(dt.natural_key()))

    def test_natural_slug(self):
        """Test the natural_slug default implementation with some representative models."""
        mfr = Manufacturer.objects.first()
        self.assertEqual(mfr.natural_slug, construct_natural_slug(mfr.natural_key(), pk=mfr.pk))
        dt = DeviceType.objects.first()
        self.assertEqual(dt.natural_slug, construct_natural_slug(dt.natural_key(), pk=dt.pk))

    def test_natural_key_field_lookups(self):
        """Test the natural_key_field_lookups default implementation with some representative models."""
        self.assertEqual(Manufacturer.natural_key_field_lookups, ["name"])
        self.assertEqual(DeviceType.natural_key_field_lookups, ["manufacturer__name", "model"])

    def test_natural_key_args_to_kwargs(self):
        """Test the natural_key_args_to_kwargs() default implementation with some representative models."""
        self.assertEqual(Manufacturer.natural_key_args_to_kwargs(["myname"]), {"name": "myname"})
        self.assertEqual(
            DeviceType.natural_key_args_to_kwargs(["mymanufacturer", "mymodel"]),
            {"manufacturer__name": "mymanufacturer", "model": "mymodel"},
        )

    def test__content_type(self):
        """
        Verify that the ContentType of the object is cached.
        """
        self.assertEqual(Manufacturer._content_type, Manufacturer._content_type_cached)

    @override_settings(CONTENT_TYPE_CACHE_TIMEOUT=2)
    def test__content_type_caching_enabled(self):
        """
        Verify that the ContentType of the object is cached.
        """

        # Ensure the cache is empty from previous tests
        cache.delete(Manufacturer._content_type_cache_key)

        with patch.object(Manufacturer, "_content_type", return_value=True) as mock__content_type:
            Manufacturer._content_type_cached
            Manufacturer._content_type_cached
            Manufacturer._content_type_cached
            self.assertEqual(mock__content_type.call_count, 1)

            time.sleep(3)  # Let the cache expire

            Manufacturer._content_type_cached
            self.assertEqual(mock__content_type.call_count, 2)

        # Clean-up after ourselves
        cache.delete(Manufacturer._content_type_cache_key)

    @override_settings(CONTENT_TYPE_CACHE_TIMEOUT=0)
    def test__content_type_caching_disabled(self):
        """
        Verify that the ContentType of the object is not cached.
        """

        # Ensure the cache is empty from previous tests
        cache.delete(Manufacturer._content_type_cache_key)

        with patch.object(Manufacturer, "_content_type", return_value=True) as mock__content_type:
            Manufacturer._content_type_cached
            Manufacturer._content_type_cached
            Manufacturer(mock__content_type.call_count, 2)


class TreeModelTestCase(TestCase):
    """Tests for the behavior of tree models, using Location as a representative model."""

    def test_values(self):
        """Test that `.values()` works properly (https://github.com/nautobot/nautobot/issues/4812)."""
        queryset = Location.objects.filter(name="Campus-01")
        instance = queryset.first()
        values_dict = queryset.values().first()
        model_dict = queryset.first().__dict__
        values_subset_dict = queryset.values("id", "name", "last_updated").first()

        for key, value in values_dict.items():
            with self.subTest(description="values()", key=key):
                self.assertEqual(value, getattr(instance, key))

        for key, value in model_dict.items():
            if key.startswith("_"):
                continue
            with self.subTest(description="__dict__", key=key):
                self.assertEqual(value, getattr(instance, key))

        for key, value in values_subset_dict.items():
            with self.subTest(description="values(key, key, key...)", key=key):
                self.assertEqual(value, getattr(instance, key))

    def test_tree_max_depth(self):
        """Test that tree_max_depth() and the max_depth cached property are calculated correctly."""
        max_tree_depth = max(loc.tree_depth for loc in Location.objects.all().with_tree_fields())
        self.assertEqual(max_tree_depth, Location.objects.all().max_tree_depth())
        self.assertEqual(max_tree_depth, Location.objects.max_depth)

        # Add a new tree so that the max depth increases
        location_type = LocationType.objects.get(name="Campus")  # root type and infinitely nestable
        status = Status.objects.get_for_model(Location).first()
        loc = None
        for i in range(max_tree_depth + 2):
            loc = Location.objects.create(
                name=f"Nested Campus {i}", parent=loc, location_type=location_type, status=status
            )
        self.assertEqual(max_tree_depth + 1, Location.objects.all().max_tree_depth())
        self.assertEqual(max_tree_depth + 1, Location.objects.max_depth)

        # Delete the most-nested location so that the max depth decreases
        loc.delete()
        self.assertEqual(max_tree_depth, Location.objects.all().max_tree_depth())
        self.assertEqual(max_tree_depth, Location.objects.max_depth)


class EmptyQuerySetShortCircuitTestCase(TestCase):
    """`RestrictedQuerySet` must answer for an empty-by-construction queryset *identically* to Django.

    `_fetch_all()` and `exists()` skip the database when `query.is_empty()`, because evaluating a
    `.none()` queryset otherwise builds and compiles SQL only to discard it at `EmptyResultSet`
    (measured 1033us against 12us for the `is_empty()` check). Django's
    `ModelMultipleChoiceField.clean()` returns `queryset.none()` for every filter left blank, so
    this path is taken constantly.

    `is_empty()` is a *sufficient* condition for "no rows", not a necessary one -- `filter(pk__in=[])`
    raises `EmptyResultSet` from the `In` lookup with no `NothingNode`, so the short-circuit
    correctly declines to fire there. What must never happen is the reverse: `is_empty()` true for a
    queryset that has rows, which would silently return nothing.

    So these tests compare against Django's own implementation rather than against an expected
    count. Asserting "no rows when is_empty()" would be circular -- the override under test is what
    produces that answer.
    """

    @classmethod
    def setUpTestData(cls):
        cls.location_type = LocationType.objects.get(name="Campus")
        cls.status = Status.objects.get_for_model(Location).first()
        cls.locations = [
            Location.objects.create(
                name=f"empty-shortcircuit-{i}",
                location_type=cls.location_type,
                status=cls.status,
            )
            for i in range(3)
        ]

    def _shapes(self):
        """Queryset shapes spanning what `is_empty()` has to judge, empty and non-empty alike."""
        first, second = self.locations[0].pk, self.locations[1].pk
        return [
            ("all", Location.objects.all()),
            ("filter with matches", Location.objects.filter(pk=first)),
            ("filter without matches", Location.objects.filter(name="empty-shortcircuit-absent")),
            ("none", Location.objects.none()),
            ("none then filter", Location.objects.none().filter(name="empty-shortcircuit-0")),
            ("filter then none", Location.objects.filter(pk=first).none()),
            # No NothingNode: EmptyResultSet comes from the `In` lookup, so is_empty() is False.
            ("pk__in empty list", Location.objects.filter(pk__in=[])),
            ("exclude pk__in empty list", Location.objects.exclude(pk__in=[])),
            ("negated Q over empty in", Location.objects.filter(~Q(pk__in=[]))),
            # Combinator queries compile through get_combinator_sql, which does not apply the outer
            # WHERE at all -- the one shape where an outer NothingNode and a non-empty branch could
            # in principle disagree.
            ("union of two non-empty", Location.objects.filter(pk=first).union(Location.objects.filter(pk=second))),
            ("union with an empty branch", Location.objects.none().union(Location.objects.filter(pk=first))),
            ("union of two empty", Location.objects.none().union(Location.objects.none())),
            ("values on none", Location.objects.none().values("pk")),
            ("values_list on none", Location.objects.none().values_list("pk", flat=True)),
            ("values on all", Location.objects.filter(pk=first).values("pk")),
        ]

    def test_rows_match_djangos_own_fetch_all(self):
        """Every shape yields exactly what Django's unpatched `_fetch_all` yields."""
        for label, queryset in self._shapes():
            with self.subTest(shape=label):
                reference = queryset.all()
                DjangoQuerySet._fetch_all(reference)
                self.assertEqual(
                    list(queryset),
                    list(reference._result_cache),
                    f"the short-circuit changed the rows returned for the {label!r} shape",
                )

    def test_exists_matches_djangos_own_exists(self):
        """Every shape's `exists()` agrees with Django's unpatched implementation."""
        for label, queryset in self._shapes():
            with self.subTest(shape=label):
                self.assertEqual(
                    queryset.exists(),
                    DjangoQuerySet.exists(queryset.all()),
                    f"the short-circuit changed exists() for the {label!r} shape",
                )

    def test_is_empty_is_never_true_for_a_queryset_with_rows(self):
        """The soundness property the short-circuit rests on, checked against Django directly."""
        for label, queryset in self._shapes():
            with self.subTest(shape=label):
                if queryset.query.is_empty():
                    reference = queryset.all()
                    DjangoQuerySet._fetch_all(reference)
                    self.assertEqual(
                        list(reference._result_cache),
                        [],
                        f"is_empty() claimed the {label!r} shape was empty, but Django returned rows",
                    )

    def test_short_circuit_reaches_no_compiler(self):
        """The point of the change: no *compilation*, which a query counter cannot see.

        Counting executed queries proves nothing here. Django catches `EmptyResultSet` inside
        `execute_sql`, so the unguarded path also executed zero queries -- it just built and
        compiled one first, at ~400us a time. Only a compiler counter distinguishes the two, and
        without one this test would pass with the guards removed.

        All four entry points are covered, because each reaches the compiler by its own route:
        `_fetch_all` (via `list()`), `exists()`, `iterator()` and `count()`.
        """
        calls = []

        original_as_sql = SQLCompiler.as_sql

        def counting_as_sql(compiler_self, *args, **kwargs):
            calls.append(compiler_self.query.model.__name__)
            return original_as_sql(compiler_self, *args, **kwargs)

        with patch.object(SQLCompiler, "as_sql", counting_as_sql):
            self.assertEqual(list(Location.objects.none()), [])
            self.assertFalse(Location.objects.none().exists())
            self.assertEqual(Location.objects.none().count(), 0)
            self.assertEqual(list(Location.objects.none().iterator()), [])

        self.assertEqual(calls, [], "an empty-by-construction queryset reached the SQL compiler")

    def test_a_real_queryset_still_reaches_the_compiler(self):
        """The negative control: the guards must not swallow a queryset that has work to do."""
        calls = []

        original_as_sql = SQLCompiler.as_sql

        def counting_as_sql(compiler_self, *args, **kwargs):
            calls.append(compiler_self.query.model.__name__)
            return original_as_sql(compiler_self, *args, **kwargs)

        with patch.object(SQLCompiler, "as_sql", counting_as_sql):
            self.assertEqual(len(list(Location.objects.filter(pk=self.locations[0].pk))), 1)
            self.assertTrue(Location.objects.filter(pk=self.locations[0].pk).exists())
            self.assertEqual(Location.objects.filter(pk=self.locations[0].pk).count(), 1)

        self.assertEqual(calls, ["Location", "Location", "Location"])

    def test_prefetch_lookups_are_still_honoured(self):
        """`_fetch_all` defers to the parent, which must still run the prefetch step."""
        queryset = Location.objects.none().prefetch_related("children")
        self.assertEqual(list(queryset), [])
        self.assertTrue(queryset._prefetch_done)


class RestrictedQuerySetTestCase(TestCase):
    """Tests for RestrictedQuerySet.restrict() and check_perms()."""

    @classmethod
    def setUpTestData(cls):
        cls.location_type = LocationType.objects.get(name="Campus")
        cls.status = Status.objects.get_for_model(Location).first()
        cls.locations = [
            Location.objects.create(
                name=f"restrict-test-{i}",
                location_type=cls.location_type,
                status=cls.status,
            )
            for i in range(3)
        ]

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_superuser_returns_all(self):
        """Superusers should bypass all permission restrictions."""
        self.user.is_superuser = True
        self.user.save()
        qs = Location.objects.restrict(self.user, "view")
        self.assertTrue(qs.filter(pk=self.locations[0].pk).exists())

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_unauthenticated_returns_none(self):
        """An unauthenticated/anonymous user should get an empty queryset."""

        anon = AnonymousUser()
        qs = Location.objects.restrict(anon, "view")
        self.assertEqual(qs.count(), 0)

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_no_permission_returns_none(self):
        """A user with no relevant permissions should get an empty queryset."""
        qs = Location.objects.restrict(self.user, "view")
        self.assertEqual(qs.count(), 0)

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_unconstrained_permission(self):
        """An ObjectPermission with null constraints should return all objects of the model."""
        self.add_permissions("dcim.view_location")
        qs = Location.objects.restrict(self.user, "view")
        self.assertEqual(qs.count(), Location.objects.count())

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_with_simple_constraint(self):
        """An ObjectPermission with a name constraint should filter correctly."""
        self.add_permissions("dcim.view_location", constraints={"name": self.locations[0].name})
        qs = Location.objects.restrict(self.user, "view")
        self.assertEqual(list(qs), [self.locations[0]])

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_with_multiple_constraint_sets(self):
        """An ObjectPermission with a list of constraints (OR'd together) should return the union."""
        self.add_permissions(
            "dcim.view_location",
            constraints=[
                {"name": self.locations[0].name},
                {"name": self.locations[1].name},
            ],
        )
        qs = Location.objects.restrict(self.user, "view")
        self.assertEqual(set(qs), {self.locations[0], self.locations[1]})

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_with_tag_constraint_single_matching_tag(self):
        """A tag-based constraint should work when an object has a single matching tag."""
        location_ct = ContentType.objects.get_for_model(Location)
        tag_ = Tag.objects.create(name="PAN_site1")
        tag_.content_types.add(location_ct)

        self.locations[0].tags.add(tag_)

        self.add_permissions("dcim.view_location", constraints={"tags__name__regex": "^PAN_.+$"})
        qs = Location.objects.restrict(self.user, "view")
        self.assertEqual(list(qs), [self.locations[0]])

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_with_tag_constraint_multiple_matching_tags_no_duplicates(self):
        """
        A tag-based constraint should not return duplicate objects when an object has multiple
        tags that all match the constraint regex.

        Regression test for https://github.com/nautobot/nautobot/issues/8690
        """
        location_ct = ContentType.objects.get_for_model(Location)
        tag1 = Tag.objects.create(name="PAN_XXX")
        tag1.content_types.add(location_ct)
        tag2 = Tag.objects.create(name="OT_PAN_XXX")
        tag2.content_types.add(location_ct)

        # Tag one location with BOTH matching tags
        self.locations[0].tags.add(tag1, tag2)

        self.add_permissions("dcim.view_location", constraints={"tags__name__regex": "^(PAN_|OT_PAN_).+$"})
        qs = Location.objects.restrict(self.user, "view")

        # The queryset should contain the location exactly once, not once per matching tag
        self.assertEqual(qs.count(), 1)
        self.assertEqual(list(qs), [self.locations[0]])

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_check_perms_with_tag_constraint_multiple_matching_tags(self):
        """
        check_perms() should return True (not raise MultipleObjectsReturned) when an object
        has multiple tags matching the permission constraint.

        Regression test for https://github.com/nautobot/nautobot/issues/8690
        """
        location_ct = ContentType.objects.get_for_model(Location)
        tag1 = Tag.objects.create(name="PAN_YYY")
        tag1.content_types.add(location_ct)
        tag2 = Tag.objects.create(name="OT_PAN_YYY")
        tag2.content_types.add(location_ct)

        self.locations[0].tags.add(tag1, tag2)

        self.add_permissions(
            "dcim.view_location",
            "dcim.change_location",
            constraints={"tags__name__regex": "^(PAN_|OT_PAN_).+$"},
        )

        # check_perms should work without error for both actions
        self.assertTrue(Location.objects.check_perms(self.user, instance=self.locations[0], action="view"))
        self.assertTrue(Location.objects.check_perms(self.user, instance=self.locations[0], action="change"))
        # An untagged location should not be permitted
        self.assertFalse(Location.objects.check_perms(self.user, instance=self.locations[1], action="view"))

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_with_tag_constraint_mixed_matching_and_nonmatching(self):
        """Only objects whose tags match the constraint should be returned, regardless of other tags."""
        location_ct = ContentType.objects.get_for_model(Location)
        matching_tag = Tag.objects.create(name="PAN_match")
        matching_tag.content_types.add(location_ct)
        nonmatching_tag = Tag.objects.create(name="unrelated_tag")
        nonmatching_tag.content_types.add(location_ct)

        # Location 0: has a matching tag
        self.locations[0].tags.add(matching_tag)
        # Location 1: has only a non-matching tag
        self.locations[1].tags.add(nonmatching_tag)
        # Location 2: no tags

        self.add_permissions("dcim.view_location", constraints={"tags__name__regex": "^PAN_.+$"})
        qs = Location.objects.restrict(self.user, "view")
        self.assertEqual(list(qs), [self.locations[0]])

    @override_settings(EXEMPT_VIEW_PERMISSIONS=["dcim.location"])
    def test_restrict_exempt_permission(self):
        """Exempt view permissions should bypass restriction."""
        qs = Location.objects.restrict(self.user, "view")
        self.assertEqual(qs.count(), Location.objects.count())

    @override_settings(EXEMPT_VIEW_PERMISSIONS=[])
    def test_restrict_action_scoping(self):
        """A permission for 'view' should not grant 'change' access."""
        self.add_permissions("dcim.view_location")
        view_qs = Location.objects.restrict(self.user, "view")
        change_qs = Location.objects.restrict(self.user, "change")
        self.assertGreater(view_qs.count(), 0)
        self.assertEqual(change_qs.count(), 0)


class NaturalKeyFieldLookupsCacheScopeTestCase(TestCase):
    """The request-scoped caches must not outlive their scope, and must not change answers.

    `cache_natural_key_field_lookups()` memoizes a `classproperty` whose value
    depends on live state -- Location's on the depth of the Location tree,
    Device's on a Constance setting -- so it is correct only for as long as
    that state cannot change, which is one serialization pass. Nothing tested
    that the scope actually closes.

    Two failures are worth guarding separately. A cache that leaks past its
    scope goes stale and serves a wrong natural key, which surfaces as a
    composite key that no longer resolves. A cache that returns a different
    answer from the uncached path is wrong immediately. The first is a leak
    test, the second a differential test.
    """

    def test_no_cache_outside_the_scope(self):
        self.assertIsNone(model_utils._natural_key_field_lookups_cache.get())
        self.assertIsNone(model_utils._serializer_instance_cache.get())

    def test_scope_creates_and_removes_the_cache(self):
        with cache_natural_key_field_lookups():
            self.assertIsInstance(model_utils._natural_key_field_lookups_cache.get(), dict)
            self.assertIsInstance(model_utils._serializer_instance_cache.get(), dict)
        self.assertIsNone(model_utils._natural_key_field_lookups_cache.get())
        self.assertIsNone(model_utils._serializer_instance_cache.get())

    def test_scope_is_removed_even_when_the_body_raises(self):
        """A leak on the exception path is the one nobody notices until it is stale."""
        with self.assertRaises(ValueError), cache_natural_key_field_lookups():
            self.assertIsInstance(model_utils._natural_key_field_lookups_cache.get(), dict)
            raise ValueError("deliberate")
        self.assertIsNone(model_utils._natural_key_field_lookups_cache.get())
        self.assertIsNone(model_utils._serializer_instance_cache.get())

    def test_a_nested_scope_reuses_the_outer_cache(self):
        """Documented behaviour: a caller wrapping a whole batch keeps its cache for every object."""
        with cache_natural_key_field_lookups():
            outer = model_utils._natural_key_field_lookups_cache.get()
            outer["sentinel"] = "kept"
            with cache_natural_key_field_lookups():
                self.assertIs(model_utils._natural_key_field_lookups_cache.get(), outer)
                self.assertEqual(model_utils._natural_key_field_lookups_cache.get()["sentinel"], "kept")
            # The inner scope must not have reset the outer one on its way out.
            self.assertIs(model_utils._natural_key_field_lookups_cache.get(), outer)
        self.assertIsNone(model_utils._natural_key_field_lookups_cache.get())

    def test_cached_lookups_equal_uncached_lookups(self):
        """The cache must not change the answer, only how often it is computed."""
        for model in (Device, Location):
            with self.subTest(model=model.__name__):
                uncached = list(model.natural_key_field_lookups)
                with cache_natural_key_field_lookups():
                    first = list(model.natural_key_field_lookups)
                    second = list(model.natural_key_field_lookups)
                self.assertEqual(uncached, first)
                self.assertEqual(first, second)
                self.assertEqual(uncached, list(model.natural_key_field_lookups))
