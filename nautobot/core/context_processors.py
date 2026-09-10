import gzip
import hashlib
import json
from urllib.parse import urlparse

from django.conf import settings as django_settings
from django.core.cache import cache
from django.template import TemplateDoesNotExist
from django.template.loader import get_template, render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils.safestring import mark_safe

import nautobot
from nautobot.core.settings_funcs import sso_auth_enabled
from nautobot.core.templatetags.helpers import has_one_or_more_perms
from nautobot.core.utils import lookup
from nautobot.core.utils.config import ExposedSettings
from nautobot.extras.registry import registry


def get_saml_idp():
    """
    Context function to provide the key for the first IDP configured for SAML.

    If the configured SAML IDP is `google`, this returns `google`.

    If SAML is not configured, this returns an empty string.
    """

    idp_map = getattr(django_settings, "SOCIAL_AUTH_SAML_ENABLED_IDPS", None)

    # We will only retrieve the first and only IDP defined as we cannot support
    # more than a single IDP for SAML at this time until we come up with a more
    # robust login system.
    value = ""
    if idp_map is not None:
        try:
            value = next(iter(idp_map.keys()))
        except IndexError:
            pass

    return value


def settings(request):
    """
    Expose an allowlisted, non-sensitive subset of Django settings in the template context.

    Access is limited by `ExposedSettings`. Example: {{ settings.VERSION }}
    """
    root_template = "base_django.html"
    return {
        "settings": ExposedSettings(),
        "root_template": root_template,
    }


class NavMenuDict(dict):
    """Because this is a large dictionary, it tends to flood the Django debug toolbar with its contents."""

    def __repr__(self):
        if django_settings.DEBUG:
            return "<NavMenu dict>"
        return super().__repr__()


# The two navbar-favorites URL names take no arguments, so their reversal is fixed for the
# life of the process: `/user/navbar-favorites/` and `/user/navbar-favorites/delete/` are
# literal paths with no captured parameters. `inc/nav_menu.html` reversed them once per menu
# item -- 246 and 123 times respectively across 123 items, 369 of a page's 387 reversals --
# at ~51us each, for two strings that cannot change. Measured: 387 reversals produced 18
# distinct URLs, all argument-free, costing 23.4ms of a 69ms chrome-only response.
#
# Resolved on first use rather than at import, because the URL conf is not loadable at import
# time and the script prefix is not known until a request is in flight. `reverse_lazy` does not
# help: `django.utils.functional.lazy` re-invokes on every coercion, so a lazy URL in a loop
# costs the same 51us as a direct one.
_NAVBAR_FAVORITES_URLS = None


def _navbar_favorites_urls():
    """Reverse the two argument-free navbar-favorites URLs once per process."""
    global _NAVBAR_FAVORITES_URLS
    if _NAVBAR_FAVORITES_URLS is None:
        _NAVBAR_FAVORITES_URLS = {
            "navbar_favorites_add_url": reverse("user:navbar_favorites_add"),
            "navbar_favorites_delete_url": reverse("user:navbar_favorites_delete"),
        }
    return _NAVBAR_FAVORITES_URLS


# Caching the rendered sidenav. `inc/nav_menu.html`'s item loop costs ~20.4ms and 267KB on every
# chrome-bearing request -- 67% of a 401KB device list page -- and finding 57 established there is
# no hot spot inside it: 160 compiled nodes expand to 3,629 node renders at ~7us, which is Django
# interpreting the template. The only levers are fewer nodes or fewer renders, and this is the
# second.
#
# The cache key is (user, registry, permission outcomes):
#
#   user.pk        Isolation, structurally rather than by fingerprint correctness. Sharing entries
#                  between users with equal permissions is worth almost nothing -- the key only
#                  changes on a restart or a permission grant, so per-user keying costs one miss
#                  per user per invalidation against hundreds of hits -- while the isolation
#                  property is the one worth guaranteeing. Username is deliberately not used: it
#                  is mutable, so a rename would orphan an entry rather than invalidate it.
#   registry       Changes when an app is installed or removed. Apps register menu items during
#                  `AppConfig.ready()`, so this cannot change without a process restart, and one
#                  fingerprint per process is enough.
#   permissions    The ~180 `has_one_or_more_perms` outcomes, which the menu is a pure function of.
#                  Identical outcomes imply an identical menu definitionally. Hashing the user's
#                  ObjectPermission rows instead would over-fragment: two users whose permissions
#                  are spelled differently but grant the same access would get different keys and
#                  identical menus, and constraints affect which *objects* a user sees rather than
#                  which menu items.
#
# Recomputed every request, so there is no invalidation logic: a permission grant produces a
# different key on the next page load.
#   template       The fragment markup itself. The registry fingerprint covers *which* items are
#                  registered, not *how* they are rendered, so an upgrade that edits
#                  `inc/nav_menu_items.html` without touching the registry would serve the
#                  previous release's markup for up to NAV_MENU_CACHE_TTL. Redis outlives the
#                  process, so restarting does not clear it and neither does a deploy.
NAV_MENU_CACHE_TTL = 24 * 60 * 60
NAV_MENU_ITEMS_TEMPLATE = "inc/nav_menu_items.html"
_REGISTRY_FINGERPRINT = None
_TEMPLATE_FINGERPRINT = None


def _template_fingerprint():
    """Fingerprint the fragment template's source, once per process."""
    global _TEMPLATE_FINGERPRINT
    if _TEMPLATE_FINGERPRINT is None:
        try:
            source = get_template(NAV_MENU_ITEMS_TEMPLATE).template.source
        except (AttributeError, TemplateDoesNotExist):
            # A loader that does not expose `.source` (or a template served from a non-file
            # backend) leaves nothing to hash. Fall back to the release version, which at least
            # separates one Nautobot release from the next.
            source = nautobot.__version__
        _TEMPLATE_FINGERPRINT = hashlib.sha256(source.encode()).hexdigest()[:16]
    return _TEMPLATE_FINGERPRINT


def _registry_fingerprint():
    """Fingerprint the menu registry and installed app list, once per process."""
    global _REGISTRY_FINGERPRINT
    if _REGISTRY_FINGERPRINT is None:
        payload = json.dumps(
            {
                "tabs": {
                    tab_name: {
                        "icon": tab.get("icon"),
                        "groups": {
                            group_name: sorted(group["items"])
                            for group_name, group in sorted(tab["groups"].items())
                        },
                    }
                    for tab_name, tab in sorted(registry["nav_menu"]["tabs"].items())
                },
                "apps": sorted(django_settings.PLUGINS),
            },
            sort_keys=True,
        )
        _REGISTRY_FINGERPRINT = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return _REGISTRY_FINGERPRINT


def _permission_fingerprint(outcomes):
    """Fingerprint the permission-check outcomes collected while filtering the menu.

    `outcomes` is a list of `(path, allowed)`. Sorted by path before packing, because dict order is
    insertion order and therefore depends on app `ready()` sequence -- two workers disagreeing
    would cause cache misses rather than wrong menus, but sorting is free.
    """
    bits = bytearray((len(outcomes) + 7) // 8)
    for index, (_, allowed) in enumerate(sorted(outcomes)):
        if allowed:
            bits[index // 8] |= 1 << (index % 8)
    return hashlib.sha256(bytes(bits)).hexdigest()[:16]


def _nav_menu_items_html(request, nav_menu_object, outcomes):
    """Render the sidenav item loop, from cache when possible.

    The cached fragment carries no per-request state: the active-item class and the favourite-star
    class are both applied client-side, from `nav_menu_active_link` and `nav_menu_favorites`. That
    is what lets one entry serve every request a user makes, rather than one per (user, page).
    """
    # `render_to_string` with an explicit dict builds a plain `Context`, so no context processor
    # runs and anything the fragment needs must be passed here. The favourites URLs were omitted
    # in the first cut of this function: Django resolves a missing variable to the empty string,
    # so every item rendered `hx-post=""` and the star buttons posted to the current page. The
    # page still looked correct and was 8,039 bytes smaller, which read as a saving for three
    # rounds of measurement before the byte delta was traced. Add to this dict, never assume.
    context = {"nav_menu": nav_menu_object, **_navbar_favorites_urls()}

    if not getattr(django_settings, "NAUTOBOT_NAV_MENU_CACHE_ENABLED", True):
        return mark_safe(render_to_string(NAV_MENU_ITEMS_TEMPLATE, context))

    key = (
        f"nav_menu_items:{request.user.pk}:{_template_fingerprint()}"
        f":{_registry_fingerprint()}:{_permission_fingerprint(outcomes)}"
    )
    compressed = cache.get(key)
    if compressed is not None:
        return mark_safe(gzip.decompress(compressed).decode())

    html = render_to_string(NAV_MENU_ITEMS_TEMPLATE, context)
    cache.set(key, gzip.compress(html.encode()), NAV_MENU_CACHE_TTL)
    return mark_safe(html)


def nav_menu(request):
    """
    Expose nav menu data for navigation and global search.

    The UI component framework renders a detail page by calling
    `nautobot.core.ui.utils.render_component_template()` once per component per pass, and each of
    those calls hands `request` to `Template.render()`. Django answers by building a fresh
    `RequestContext` and re-running every context processor -- 70 times on a rack detail page,
    81 on a device detail page -- so this function and its ~176 permission checks were repeated
    for a result that cannot change within a single request.

    Memoize on the request object. The menu is a pure function of `registry["nav_menu"]`,
    `settings.PLUGINS`, `request.user`, `request.resolver_match` and the request's URL. The first
    two are fixed for the life of the process; the rest are fixed for the life of the request.
    Storing the result on the request instance scopes the cache to exactly one request and
    therefore exactly one user: nothing is keyed by user ID, nothing is stored on the user, the
    module, or a shared cache, and no entry outlives the response. Two users cannot observe each
    other's menu.
    """
    cached = getattr(request, "_nautobot_nav_menu_context", None)
    if cached is not None:
        return cached
    result = _build_nav_menu(request)
    try:
        request._nautobot_nav_menu_context = result
    except AttributeError:  # pragma: no cover - exotic request objects that reject attributes
        pass
    return result


def _build_nav_menu(request):
    """
    Build the nav menu context for this request. See `nav_menu()`, which memoizes this.

    Also, indicate whether `"nautobot_version_control"` app is installed in order to render branch picker in nav menu.
    """
    active_link = (None, None, None)
    related_list_view_link = None
    if request.resolver_match:
        # Try to map requested page `view_name` to a specific `model` via `lookup.get_model_for_view_name`.
        try:
            model = lookup.get_model_for_view_name(request.resolver_match.view_name)
        except ValueError:
            model = None

        # If model mapping above fails, fall back to deriving a `model` from requested page `view_class` `queryset`.
        if not model:
            view_func = request.resolver_match.func
            view_class = None
            if hasattr(view_func, "view_class"):  # Valid for generic Views
                view_class = view_func.view_class
            elif hasattr(view_func, "cls"):  # Valid for UI component framework ViewSets
                view_class = view_func.cls
            view_instance = view_class() if view_class else None
            queryset = getattr(view_instance, "queryset", None)
            model = getattr(queryset, "model", None)

        # If related `model` reference has been found, map it to a list view link.
        try:
            related_list_view_name = lookup.get_route_for_model(model, "list") if model else None
            related_list_view_link = reverse(related_list_view_name) if related_list_view_name else None
        except (NoReverseMatch, ValueError):
            pass

    nav_menu_object = NavMenuDict({"tabs": {}})

    if htmx_current_url := request.headers.get("HX-Current-URL"):
        current_url = urlparse(htmx_current_url).path
    else:
        current_url = request.path

    # Every permission outcome is recorded as it is evaluated, so the cache fingerprint costs one
    # walk rather than two. A denied tab short-circuits its children, so the recorded set differs
    # in membership as well as in values -- which is what makes it a faithful fingerprint: a tab
    # denied outright and a tab allowed with every child denied produce different sets.
    permission_outcomes = []

    for tab_name, tab_details in registry["nav_menu"]["tabs"].items():
        tab_allowed = not tab_details["permissions"] or has_one_or_more_perms(
            request.user, tab_details["permissions"]
        )
        permission_outcomes.append((tab_name, tab_allowed))
        if tab_allowed:
            nav_menu_object["tabs"][tab_name] = {"groups": {}, "icon": tab_details["icon"]}
            for group_name, group_details in tab_details["groups"].items():
                group_allowed = not group_details["permissions"] or has_one_or_more_perms(
                    request.user, group_details["permissions"]
                )
                permission_outcomes.append((f"{tab_name}/{group_name}", group_allowed))
                if group_allowed:
                    nav_menu_object["tabs"][tab_name]["groups"][group_name] = {"items": {}}
                    for item_link, item_details in group_details["items"].items():
                        item_allowed = not item_details["permissions"] or has_one_or_more_perms(
                            request.user, item_details["permissions"]
                        )
                        permission_outcomes.append((f"{tab_name}/{group_name}/{item_link}", item_allowed))
                        if item_allowed:
                            if item_link == current_url:  # Always prefer exact match
                                active_link = (tab_name, group_name, item_link)
                            elif None in active_link and item_link == related_list_view_link:
                                active_link = (tab_name, group_name, item_link)

                            nav_menu_object["tabs"][tab_name]["groups"][group_name]["items"][item_link] = {
                                "is_active": False,
                                "name": item_details["name"],
                                "weight": item_details["weight"],
                            }
                    if len(nav_menu_object["tabs"][tab_name]["groups"][group_name]["items"]) == 0:
                        del nav_menu_object["tabs"][tab_name]["groups"][group_name]
            if len(nav_menu_object["tabs"][tab_name]["groups"]) == 0:
                del nav_menu_object["tabs"][tab_name]

    if None not in active_link:
        nav_menu_object["tabs"][active_link[0]]["groups"][active_link[1]]["items"][active_link[2]]["is_active"] = True

    nav_menu_version_control = None
    if "nautobot_version_control" in django_settings.PLUGINS:
        from nautobot_version_control.constants import (  # pylint: disable=import-error
            DOLT_BRANCH_KEYWORD,
            DOLT_DEFAULT_BRANCH,
            DOLT_TIME_TRAVEL_KEYWORD,
        )

        nav_menu_version_control = {
            "active_branch": getattr(request, DOLT_BRANCH_KEYWORD, DOLT_DEFAULT_BRANCH),
            "active_time_travel_date": getattr(request, DOLT_TIME_TRAVEL_KEYWORD, None),
            "default_branch": DOLT_DEFAULT_BRANCH,
        }

    favorites = []
    if request.user.is_authenticated:
        favorites = list(getattr(request.user, "navbar_favorites_link_list", None) or [])

    return {
        "nav_menu": nav_menu_object,
        "nav_menu_version_control": nav_menu_version_control,
        # The item loop, rendered once per (user, registry, permission set) and reused. Carries no
        # per-request state; the two values below are applied client-side.
        "nav_menu_items_html": _nav_menu_items_html(request, nav_menu_object, permission_outcomes),
        # Resolved server-side because it is not simply "does the URL match": `active_link` above
        # falls back to the current view's related list URL, so a device *detail* page highlights
        # the device *list* item. A client comparing `location.pathname` would lose that.
        "nav_menu_active_link": active_link[2] if None not in active_link else None,
        "nav_menu_favorites": favorites,
        **_navbar_favorites_urls(),
    }


def sso_auth(request):
    """
    Expose SSO-related variables for use in generating login URL fragments for external authentication providers.
    """

    return {
        "SAML_IDP": get_saml_idp,
        "SSO_AUTH_ENABLED": lambda: sso_auth_enabled(django_settings.AUTHENTICATION_BACKENDS),
    }
