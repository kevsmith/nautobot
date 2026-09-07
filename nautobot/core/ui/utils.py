from typing import Optional

from django.db.models import Model
from django.template import Context
from django.template.loader import get_template


def flatten_context(context) -> dict:
    """
    If the given context is already a dict, return it unmodified; if it is a Context, flatten it to a dict.

    This is working around a bug (not sure if in Django or in our code) where, if a `Context`'s `dicts` contain
    a `RequestContext`, calling `Context.flatten()` throws an exception.
    """
    if isinstance(context, dict):
        return context
    flat = {}
    for d in context.dicts:
        if isinstance(d, dict):
            flat.update(d)
        else:
            flat.update(flatten_context(d))
    return flat


def render_component_template(template_path: str, context: Context, **kwargs) -> str:
    """
    Render the template located at the given path with the given context, possibly augmented via additional kwargs.

    Args:
        template_path (str): Path to the template to render, for example `"components/tab/label_wrapper.html"`.
        context (Context): Rendering context for the template
        **kwargs (dict): Additional key/value pairs to extend the context with for this specific template.

    Examples:
        >>> render_component_template(self.label_wrapper_template_path, context, tab_id=self.tab_id, label="Hello")
    """
    with context.update(kwargs):
        flat_context = flatten_context(context)
        return get_template(template_path).render(flat_context, request=flat_context.get("request"))


def get_absolute_url(value: Optional[Model]) -> str:
    """
    Function to retrieve just absolute url to the given model instance.

    Args:
        value (Optional[django.db.models.Model]): Instance of a Django model or None.

    Returns:
        (str): url to the object if it defines get_absolute_url(), empty string otherwise.
    """
    if value is None:
        return ""

    if hasattr(value, "get_absolute_url"):
        try:
            return value.get_absolute_url()
        except AttributeError:
            return ""

    return ""


def get_render_cache(context) -> Optional[dict]:
    """
    Return a per-HTTP-request memoization dict for UI component rendering, or None if one isn't available.

    The UI framework renders a given detail page in several passes over the same component tree (tab labels,
    table-config forms, tab contents), which means the same `Component` methods get called repeatedly with
    equivalent context. Anything cached here is scoped to a single request and discarded with it.
    """
    request = context.get("request") if context is not None else None
    if request is None:
        return None
    cache = getattr(request, "_nautobot_ui_render_cache", None)
    if cache is None:
        cache = {}
        try:
            request._nautobot_ui_render_cache = cache
        except AttributeError:  # pragma: no cover - exotic request objects
            return None
    return cache
