import hashlib
import os
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings, RequestFactory, tag, TestCase
from django.urls import resolve, reverse

import nautobot
from nautobot.core import context_processors
from nautobot.core.apps import NavMenuTab
from nautobot.core.choices import ButtonActionColorChoices, ButtonActionIconChoices
from nautobot.core.context_processors import nav_menu
from nautobot.core.testing.utils import get_expected_menu_item_name
from nautobot.core.ui.choices import NavigationIconChoices, NavigationWeightChoices
from nautobot.core.utils.lookup import get_route_for_model
from nautobot.core.utils.module_loading import import_string_optional
from nautobot.core.utils.permissions import get_permission_for_model
from nautobot.dcim.models import Device
from nautobot.extras.models import Job
from nautobot.extras.registry import registry


@tag("unit")
class NavMenuTestCase(TestCase):
    """Verify correct construction of the nav menu."""

    def test_menu_item_attributes(self):
        """Verify that menu items and buttons have the correct text and expected permissions."""
        for tab in registry["nav_menu"]["tabs"]:
            for group in registry["nav_menu"]["tabs"][tab]["groups"]:
                for item_url, item_details in registry["nav_menu"]["tabs"][tab]["groups"][group]["items"].items():
                    with self.subTest(f"{tab} > {group} > {item_url}"):
                        view_func = resolve(item_url.split("?")[0]).func
                        try:
                            # NautobotUIViewSet
                            view_class = view_func.view_class
                        except AttributeError:
                            # ObjectListView
                            view_class = view_func.cls
                        try:
                            view_queryset = view_class.queryset
                            view_model = view_queryset.model

                            if item_details["name"] not in {
                                "Elevations",
                                "Example Models filtered",
                                "Interface Connections",
                                "Console Connections",
                                "Power Connections",
                                "Wireless Controllers",
                            }:
                                expected_name = get_expected_menu_item_name(view_model)
                                self.assertEqual(item_details["name"], expected_name)
                            if item_url == get_route_for_model(view_model, "list"):
                                # Not assertEqual as some menu items have additional permissions defined.
                                self.assertIn(get_permission_for_model(view_model, "view"), item_details["permissions"])
                        except AttributeError:
                            # Not a model view?
                            self.assertIn(
                                item_details["name"],
                                {"Apps Marketplace", "Installed Apps", "Interface Connections", "Device Constraints"},
                            )

                    for button, button_details in item_details["buttons"].items():
                        with self.subTest(f"{tab} > {group} > {item_url} > {button}"):
                            # Currently all core menu items should have just a single Add button
                            self.assertEqual(button, "Add")
                            self.assertEqual(
                                button_details["permissions"], {get_permission_for_model(view_model, "add")}
                            )
                            self.assertEqual(button_details["link"], get_route_for_model(view_model, "add"))
                            self.assertEqual(button_details["button_class"], ButtonActionColorChoices.ADD)
                            self.assertEqual(button_details["icon_class"], ButtonActionIconChoices.ADD)

    def test_permissions_rollup(self):
        menus = registry["nav_menu"]
        expected_perms = {}
        for tab_name, tab_details in menus["tabs"].items():
            expected_perms[tab_name] = set()
            for group_name, group_details in tab_details["groups"].items():
                expected_perms[f"{tab_name}:{group_name}"] = set()
                for item_details in group_details["items"].values():
                    item_perms = item_details["permissions"]
                    # If any item has no permissions restriction, then the group has no permissions restriction
                    if expected_perms[f"{tab_name}:{group_name}"] is None or not item_perms:
                        expected_perms[f"{tab_name}:{group_name}"] = None
                    else:
                        expected_perms[f"{tab_name}:{group_name}"] |= item_perms
                group_perms = group_details["permissions"]
                self.assertEqual(expected_perms[f"{tab_name}:{group_name}"], group_perms)
                # if any group has no permissions restriction, then the tab has no permissions restriction
                if expected_perms[tab_name] is None or not group_perms:
                    expected_perms[tab_name] = None
                else:
                    expected_perms[tab_name] |= group_perms
            self.assertEqual(expected_perms[tab_name], tab_details["permissions"])

    def test_nav_menu_tabs_have_icon_and_weight(self):
        """Ensure each NavMenuTab in every navigation.py has an icon and weight set, and any duplicates by name match."""
        tabs_by_name = {}
        for app in apps.get_app_configs():
            if not app.name.startswith("nautobot."):
                continue
            nav_path = f"{app.name}.navigation.menu_items"
            menu_items = import_string_optional(nav_path)
            if menu_items is None:
                continue
            for tab in menu_items:
                if not isinstance(tab, NavMenuTab):
                    raise TypeError(f"Expected NavMenuTab instance in {nav_path}, got {type(tab)}")
                tab_name = tab.name
                icon = tab.icon
                weight = tab.weight
                with self.subTest(tab_name=tab_name, nav_path=nav_path):
                    self.assertIsNotNone(tab_name, f"Tab in {nav_path} missing 'name'")
                    self.assertIsNotNone(icon, f"Tab '{tab_name}' in {nav_path} missing 'icon'")
                    self.assertIsNotNone(weight, f"Tab '{tab_name}' in {nav_path} missing 'weight'")
                    if tab_name in tabs_by_name:
                        prev_icon, prev_weight, prev_path = tabs_by_name[tab_name]
                        self.assertEqual(
                            icon,
                            prev_icon,
                            f"Tab '{tab_name}' has inconsistent icons: '{icon}' in {nav_path} vs '{prev_icon}' in {prev_path}",
                        )
                        self.assertEqual(
                            weight,
                            prev_weight,
                            f"Tab '{tab_name}' has inconsistent weights: '{weight}' in {nav_path} vs '{prev_weight}' in {prev_path}",
                        )
                    else:
                        tabs_by_name[tab_name] = (icon, weight, nav_path)

    def test_icon_and_weight_class_attributes_match(self):
        """
        Ensure every class attribute in NavigationIconChoices is also in NavigationWeightChoices and vice versa.
        If not, print the missing/extra attributes for easier debugging.
        """
        icon_attrs = {attr for attr in dir(NavigationIconChoices) if attr.isupper()}
        weight_attrs = {attr for attr in dir(NavigationWeightChoices) if attr.isupper()}

        only_in_icons = sorted(icon_attrs - weight_attrs)
        only_in_weights = sorted(weight_attrs - icon_attrs)

        if only_in_icons or only_in_weights:
            msg = []
            if only_in_icons:
                msg.append(f"Class attributes only in NavigationIconChoices: {only_in_icons}")
            if only_in_weights:
                msg.append(f"Class attributes only in NavigationWeightChoices: {only_in_weights}")
            self.fail("\n".join(msg))

    def test_navigation_icons_have_svg(self):
        """Ensure every NavigationIconChoices icon has a corresponding SVG file."""
        missing = []
        svg_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "project-static", "nautobot-icons")
        )
        icon_attrs = [attr for attr in dir(NavigationIconChoices) if attr.isupper() and not attr == "CHOICES"]
        for icon_attr in icon_attrs:
            icon_name = getattr(NavigationIconChoices, icon_attr)
            svg_path = os.path.join(svg_dir, f"{icon_name}.svg")
            if not os.path.isfile(svg_path):
                missing.append(svg_path)
        self.assertFalse(missing, f"Missing SVG files for NavigationIconChoices: {missing}")

    @override_settings(EXEMPT_VIEW_PERMISSIONS=["*"])
    def test_menu_item_is_active(self):
        """Verify that the correct menu item is marked as active."""
        User = get_user_model()
        user = User.objects.create(username="User 1", is_active=True, is_superuser=True)
        request_factory = RequestFactory()
        request = request_factory.get("/dcim/devices/")
        request.resolver_match = resolve("/dcim/devices/")
        request.user = user

        nav = nav_menu(request)["nav_menu"]

        # Assert that only one menu item is active
        active_items = {
            f"{tabname}-{groupname}-{itemname}": itemvalue
            for tabname, tabvalue in nav["tabs"].items()
            for groupname, groupvalue in tabvalue["groups"].items()
            for itemname, itemvalue in groupvalue["items"].items()
            if itemvalue["is_active"]
        }
        self.assertEqual(len(active_items), 1, msg=active_items.keys())

        # Assert that the correct devices menu item is active
        self.assertTrue(nav["tabs"]["Devices"]["groups"]["Devices"]["items"]["/dcim/devices/"]["is_active"])

        # Force get_model_for_view_name to return the Device model
        with patch("nautobot.core.context_processors.lookup") as mock_lookup:
            mock_lookup.get_model_for_view_name.return_value = Device
            mock_lookup.get_route_for_model.return_value = "dcim:device_list"
            request = request_factory.get("/extras/jobs/")
            request.resolver_match = resolve("/extras/jobs/")
            request.user = user

            nav = nav_menu(request)["nav_menu"]

            # Assert that only one menu item is active
            active_items = {
                f"{tabname}-{groupname}-{itemname}": itemvalue
                for tabname, tabvalue in nav["tabs"].items()
                for groupname, groupvalue in tabvalue["groups"].items()
                for itemname, itemvalue in groupvalue["items"].items()
                if itemvalue["is_active"]
            }
            self.assertEqual(len(active_items), 1, msg=active_items.keys())

            # Assert that the menu item for the requested URL is active
            self.assertTrue(nav["tabs"]["Jobs"]["groups"]["Jobs"]["items"]["/extras/jobs/"]["is_active"])

        # Force get_model_for_view_name to return the Job model
        with patch("nautobot.core.context_processors.lookup") as mock_lookup:
            mock_lookup.get_model_for_view_name.return_value = Job
            mock_lookup.get_route_for_model.return_value = "extras:job_list"
            request = request_factory.get("/dcim/devices/")
            request.resolver_match = resolve("/dcim/devices/")
            request.user = user

            nav = nav_menu(request)["nav_menu"]

            # Assert that only one menu item is active
            active_items = {
                f"{tabname}-{groupname}-{itemname}": itemvalue
                for tabname, tabvalue in nav["tabs"].items()
                for groupname, groupvalue in tabvalue["groups"].items()
                for itemname, itemvalue in groupvalue["items"].items()
                if itemvalue["is_active"]
            }
            self.assertEqual(len(active_items), 1, msg=active_items.keys())

            # Assert that the menu item for the requested URL is active
            self.assertTrue(nav["tabs"]["Devices"]["groups"]["Devices"]["items"]["/dcim/devices/"]["is_active"])


@tag("unit")
class NavMenuCacheTestCase(TestCase):
    """Verify the cached sidenav item fragment.

    The fragment is rendered by `render_to_string` with an explicit context dict, which builds a
    plain `Context` rather than a `RequestContext`. No context processor runs, so every variable the
    template needs must be passed explicitly -- and Django resolves a missing one to the empty
    string rather than raising. The first cut of the cache omitted the two favourites URLs and every
    star button rendered `hx-post=""`, posting to the current page instead of the favourites
    endpoint. Nothing failed: the page rendered, the markup was well-formed, and the only visible
    symptom was that each response was 8,039 bytes smaller, which was read as a saving across three
    rounds of A/B measurement before it was traced.
    """

    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.user = User.objects.create(username="navcache-super", is_active=True, is_superuser=True)
        self.factory = RequestFactory()

    def _fragment(self, user=None, path="/dcim/devices/"):
        request = self.factory.get(path)
        request.resolver_match = resolve(path)
        request.user = user or self.user
        return str(nav_menu(request)["nav_menu_items_html"])

    def test_fragment_carries_the_favorites_urls(self):
        """Every star button must post to the favourites endpoint, not to an empty URL."""
        fragment = self._fragment()
        add_url = reverse("user:navbar_favorites_add")
        delete_url = reverse("user:navbar_favorites_delete")

        # Counted rather than asserted with `assertIn`, so a failure prints two integers instead
        # of dumping a 200KB fragment into the test log.
        buttons = fragment.count("nb-sidenav-favorite")
        self.assertGreater(buttons, 0, "no star buttons rendered; the rest of this test is vacuous")

        for attr, url in (("hx-post", add_url), ("data-add-url", add_url), ("data-delete-url", delete_url)):
            with self.subTest(attribute=attr):
                self.assertEqual(
                    fragment.count(f'{attr}="{url}"'),
                    buttons,
                    f"{attr} is not the favourites URL on every button",
                )
                self.assertEqual(
                    fragment.count(f'{attr}=""'),
                    0,
                    f'{attr} rendered empty -- is "{url}" missing from the fragment context?',
                )

    def test_fragment_holds_no_per_request_state(self):
        """The active class and favourite state are applied client-side, so must not be stored."""
        fragment = self._fragment(path="/dcim/devices/")
        self.assertIn('href="/dcim/devices/"', fragment)  # precondition: the item is present
        self.assertNotIn("nb-sidenav-link-active", fragment)
        self.assertNotIn('class="nb-sidenav-favorite active"', fragment)

    def test_cache_hit_matches_cache_miss(self):
        """A hit decompresses to exactly what the miss stored."""
        miss = self._fragment()
        hit = self._fragment()
        self.assertEqual(miss, hit)
        self.assertGreater(len(hit), 1000)

    def test_users_with_different_permissions_get_different_fragments(self):
        """The permission fingerprint must separate a superuser from a user who sees less."""
        User = get_user_model()
        limited = User.objects.create(username="navcache-limited", is_active=True)
        self.assertNotEqual(self._fragment(user=self.user), self._fragment(user=limited))

    def test_cache_key_tracks_the_template_source(self):
        """An upgrade that edits the fragment must not serve the previous release's markup.

        The registry fingerprint covers which items are registered, not how they are rendered,
        and Redis outlives both the process and the deploy -- so without this the cache would
        hand back the old markup for up to `NAV_MENU_CACHE_TTL` after an upgrade.
        """
        first = context_processors._template_fingerprint()
        self.assertRegex(first, r"^[0-9a-f]{16}$")

        # Same source -> same fingerprint, memoised.
        self.assertEqual(first, context_processors._template_fingerprint())

        # Different source -> different fingerprint.
        with patch.object(context_processors, "_TEMPLATE_FINGERPRINT", None):
            with patch.object(context_processors, "get_template") as mock_get_template:
                mock_get_template.return_value.template.source = "<li>edited by an upgrade</li>"
                self.assertNotEqual(first, context_processors._template_fingerprint())

    def test_template_fingerprint_falls_back_to_the_release_version(self):
        """A loader that does not expose `.source` must still separate one release from the next."""
        with patch.object(context_processors, "_TEMPLATE_FINGERPRINT", None):
            with patch.object(context_processors, "get_template", side_effect=AttributeError):
                fallback = context_processors._template_fingerprint()
        self.assertRegex(fallback, r"^[0-9a-f]{16}$")
        self.assertEqual(fallback, hashlib.sha256(nautobot.__version__.encode()).hexdigest()[:16])
