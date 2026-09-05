import uuid

from django.conf import settings
from django.core.cache import cache
from django.db.models import Case, When
from django.db.models.constants import LOOKUP_SEP
from django.db.models.signals import post_delete, post_save
from tree_queries.compiler import TreeQuery
from tree_queries.models import TreeNode
from tree_queries.query import TreeManager as TreeManager_, TreeQuerySet as TreeQuerySet_

from nautobot.core.models import BaseManager, querysets
from nautobot.core.signals import invalidate_max_depth_cache
from nautobot.core.utils.cache import cache_get_or_set, construct_cache_key

# How many levels of a tree's `parent` chain to fetch per query when walking it without tree fields.
# Chains deeper than this simply take another query per additional block of this many levels.
ANCESTOR_JOIN_DEPTH = 4


class TreeQuerySet(TreeQuerySet_, querysets.RestrictedQuerySet):
    """
    Combine django-tree-queries' TreeQuerySet with our RestrictedQuerySet for permissions enforcement.
    """

    def ancestors(self, of, *, include_self=False):
        """Custom ancestors method for optimization purposes.

        Dynamically computes ancestors either through the tree or through the `parent` foreign key depending on whether
        tree fields are present on `of`.
        """

        # If `of` is a UUID, i.e. pk, retrieve the corresponding model instance with tree fields disabled.
        if isinstance(of, uuid.UUID):
            of = self.model.objects.without_tree_fields().get(pk=of)

        # If `of` has `tree_depth` defined, i.e. if it was retrieved from the database on a queryset where tree fields
        # were enabled (see `TreeQuerySet.with_tree_fields` and `TreeQuerySet.without_tree_fields`), use the default
        # implementation from `tree_queries.query.TreeQuerySet`.
        if hasattr(of, "tree_depth"):
            return super().ancestors(of, include_self=include_self)

        # In the other case, traverse the `parent` foreign key until the root. Following that foreign key one
        # node at a time costs a query per tree level, so each cache miss pulls the next ANCESTOR_JOIN_DEPTH
        # levels in a single query instead; links whose `parent` is already cached on the instance (because
        # something else walked the same chain earlier in this request) cost nothing.
        ancestors = []
        node = of
        parent_field = self.model._meta.get_field("parent")
        while node.parent_id is not None:
            if "parent" in node._state.fields_cache:
                node = node.parent
            else:
                fetched = (
                    self.model.objects.without_tree_fields()
                    .select_related(LOOKUP_SEP.join(["parent"] * ANCESTOR_JOIN_DEPTH))
                    .get(pk=node.parent_id)
                )
                # Populate the foreign-key cache the way plain `node.parent` attribute access would have, so
                # that walking this chain leaves callers no worse off than before. Without this, later
                # `instance.parent` reads that used to be free become queries again.
                parent_field.set_cached_value(node, fetched)
                node = fetched
            # Insert in reverse order so that the root is the first element
            ancestors.insert(0, node)
        if include_self:
            ancestors.append(of)
        ancestor_pks = [ancestor.pk for ancestor in ancestors]
        # Maintain API compatibility by returning a queryset instead of a list directly.
        # Reference:
        # https://stackoverflow.com/questions/4916851/django-get-a-queryset-from-array-of-ids-in-specific-order
        preserve_order = Case(*[When(pk=pk, then=position) for position, pk in enumerate(ancestor_pks)])
        return self.model.objects.without_tree_fields().filter(pk__in=ancestor_pks).order_by(preserve_order)

    def max_tree_depth(self):
        r"""
        Get the maximum tree depth of any node in this queryset.

        In most cases you should use TreeManager.max_depth instead as it's cached and this is not.

        root  - depth 0
         \
          branch  - depth 1
            \
            leaf  - depth 2

        Note that a queryset with only root nodes will return zero, and an empty queryset will also return zero.
        This is probably a bug, we should really return -1 in the case of an empty queryset, but this is
        "working as implemented" and changing it would possibly be a breaking change at this point.
        """
        deepest = self.with_tree_fields().extra(order_by=["-__tree.tree_depth"]).first()
        if deepest is not None:
            return deepest.tree_depth
        return 0

    def count(self):
        """Custom count method for optimization purposes.

        TreeQuerySet instances in Nautobot are by default with tree fields. So if somewhere tree fields aren't
        explicitly removed from the queryset and count is called, the whole tree is calculated. Since this is not
        needed, this implementation calls `without_tree_fields` before issuing the count query and `with_tree_fields`
        afterwards when applicable.
        """
        should_have_tree_fields = isinstance(self.query, TreeQuery)
        if should_have_tree_fields:
            self.without_tree_fields()
        count = super().count()
        if should_have_tree_fields:
            self.with_tree_fields()
        return count


class TreeManager(TreeManager_, BaseManager.from_queryset(TreeQuerySet)):
    """
    Extend django-tree-queries' TreeManager to incorporate RestrictedQuerySet.
    """

    _with_tree_fields = False
    use_in_migrations = True

    @property
    def max_depth_cache_key(self):
        return construct_cache_key(self, method_name="max_depth", branch_aware=True)

    @property
    def max_depth(self):
        """Cacheable version of `TreeQuerySet.max_tree_depth()`.

        Generally TreeManagers are persistent objects while TreeQuerySets are not, hence the difference in behavior.

        Within a `request_cache()` scope the value is also memoized in-process: a single request reads it once per
        object it serializes, and each of those reads would otherwise be a Redis round-trip.
        """
        # both caches are explicitly invalidated by nautobot.core.signals.invalidate_max_depth_cache as needed
        max_depth, _ = cache_get_or_set(self.max_depth_cache_key, self.max_tree_depth, timeout=None)
        return max_depth


class TreeModel(TreeNode):
    """
    Nautobot-specific base class for models that exist in a self-referential tree.
    """

    objects = TreeManager()

    class Meta:
        abstract = True

    def cacheable_descendants_pks(self, restrict_to_user=None, include_self=False):
        """Cacheable version of descendants() method, with optional permissions restriction."""
        user_id = restrict_to_user.id if restrict_to_user is not None else None
        cache_key = construct_cache_key(
            self,
            method_name="cacheable_descendants_pks",
            branch_aware=True,
            restrict_to_user=user_id,
            include_self=include_self,
        )
        pk_list = cache.get(cache_key)
        if pk_list is None:
            queryset = self.descendants(include_self=include_self)
            if restrict_to_user:
                queryset = queryset.restrict(restrict_to_user, "view")
            pk_list = list(queryset.values_list("pk", flat=True))
            # cache is explicitly invalidated by TreeModel.save() and TreeModel.delete() methods
            # However since this is a *per-instance* cache we don't want it to grow indefinitely over time.
            cache.set(cache_key, pk_list, timeout=settings.CACHES["default"].get("TIMEOUT", 300))
        return pk_list

    @property
    def display(self):
        """
        By default, TreeModels display their full ancestry for clarity.

        As this is an expensive thing to calculate, we cache it for a few seconds in the case of repeated lookups.

        When the ancestry is already loaded in memory - as `select_related("parent__parent__...")` in a list view
        or serializer arranges - the string is assembled from those instances instead. That is cheaper than the
        cache round-trip it replaces, and cannot return a value that is stale relative to what was loaded.
        """
        if not hasattr(self, "name"):
            raise NotImplementedError("default TreeModel.display implementation requires a `name` attribute!")
        names = [self.name]  # pylint: disable=no-member  # we checked with hasattr() above
        node = self
        seen_pks = {self.pk}
        while node.parent_id is not None and node.parent_id not in seen_pks:
            parent = node._state.fields_cache.get("parent")
            if parent is None:
                break  # ancestry is not fully in memory - fall through to the cached implementation below
            seen_pks.add(parent.pk)
            names.append(parent.name)
            node = parent
        else:
            return " → ".join(reversed(names))

        cache_key = construct_cache_key(self, method_name="display", branch_aware=True)
        display_str = cache.get(cache_key, "")
        if display_str:
            return display_str
        try:
            if self.parent_id is not None:
                parent_display_str = cache.get(cache_key.replace(str(self.id), str(self.parent_id)), "")
                if not parent_display_str:
                    parent_display_str = self.parent.display  # pylint: disable=no-member
                display_str = parent_display_str + " → "
        except self.DoesNotExist:
            # Expected to occur at times during bulk-delete operations
            pass
        display_str += self.name  # pylint: disable=no-member  # we checked with hasattr() above
        cache.set(cache_key, display_str, timeout=5)
        return display_str

    @property
    def siblings(self):
        return self.__class__.objects.without_tree_fields().filter(parent_id=self.parent_id).exclude(pk=self.pk)

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        post_save.connect(invalidate_max_depth_cache, sender=cls)
        post_delete.connect(invalidate_max_depth_cache, sender=cls)

    def save(self, *args, **kwargs):
        """
        On any change to the `parent` value, invalidate the `cached_descendants_pks` of our old and new ancestors.
        """
        if getattr(self, "present_in_database", False):
            old_instance = self.__class__.objects.without_tree_fields().select_related("parent").get(pk=self.pk)
            parent_changed = old_instance.parent != self.parent
        else:
            old_instance = None
            parent_changed = True

        if parent_changed and old_instance is not None:
            for ancestor in old_instance.ancestors(include_self=False):
                cache_key = construct_cache_key(ancestor, method_name="cacheable_descendants_pks", branch_aware=True)
                cache.delete_pattern(f"{cache_key}(*)")

        super().save(*args, **kwargs)

        if parent_changed:
            for ancestor in self.ancestors(include_self=False):
                cache_key = construct_cache_key(ancestor, method_name="cacheable_descendants_pks", branch_aware=True)
                cache.delete_pattern(f"{cache_key}(*)")

    def delete(self, *args, **kwargs):
        for ancestor in self.ancestors(include_self=False):
            cache_key = construct_cache_key(ancestor, method_name="cacheable_descendants_pks", branch_aware=True)
            cache.delete_pattern(f"{cache_key}(*)")

        return super().delete(*args, **kwargs)
