from collections.abc import Iterable
import json
from urllib.parse import urljoin

from django import forms
from django.forms.models import ModelChoiceIterator
from django.urls import get_script_prefix
from django.forms.renderers import get_default_renderer
from django.utils.safestring import mark_safe

from nautobot.core import choices as core_choices
from nautobot.core.forms import utils

__all__ = (
    "APISelect",
    "APISelectMultiple",
    "AutoPopulateWidget",
    "BulkEditNullBooleanSelect",
    "ClearableFileInput",
    "ColorSelect",
    "ContentTypeSelect",
    "DatePicker",
    "DateTimePicker",
    "NumberWithSelect",
    "SelectMultipleOrderable",
    "SelectWithDisabled",
    "SelectWithPK",
    "SlugWidget",
    "SmallTextarea",
    "StaticSelect2",
    "StaticSelect2Multiple",
    "TimePicker",
)


class SmallTextarea(forms.Textarea):
    """
    Subclass used for rendering a smaller textarea element.
    """


class SlugWidget(forms.TextInput):
    """
    Subclass TextInput and add a slug regeneration button next to the form field.
    """

    template_name = "widgets/sluginput.html"

    def get_context(self, name, value, attrs):
        custom_title = self.attrs.pop("title", None)
        context = super().get_context(name, value, attrs)
        context["widget"]["custom_title"] = custom_title
        return context


class AutoPopulateWidget(SlugWidget):
    """
    Subclass SlugWidget and add support for auto-populate JavaScript logic from `form.js`.
    """

    def get_context(self, name, value, attrs):
        attrs["data-autopopulate"] = ""
        context = super().get_context(name, value, attrs)
        return context


class ColorSelect(forms.Select):
    """
    Extends the built-in Select widget to colorize each <option>.
    """

    option_template_name = "widgets/colorselect_option.html"

    def __init__(self, *args, **kwargs):
        kwargs["choices"] = utils.add_blank_choice(core_choices.ColorChoices)
        super().__init__(*args, **kwargs)
        self.attrs["class"] = "nautobot-select2-color-picker"


class BulkEditNullBooleanSelect(forms.NullBooleanSelect):
    """
    A Select widget for NullBooleanFields
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Override the built-in choice labels
        self.choices = (
            ("1", "---------"),
            ("2", "Yes"),
            ("3", "No"),
        )
        self.attrs["class"] = "nautobot-select2-static"


class SelectMultipleOrderable(forms.SelectMultiple):
    """
    Modified the stock SelectMultiple widget to render a set of controls with draggable list group rows to enable
    ordering and checkboxes to simplify the selection process.
    """

    template_name = "widgets/select_multiple_orderable.html"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.attrs["class"] = (
            "list-group nb-draggable-container nb-select-multiple-orderable-list flex-grow-1 mx-n20 py-16"
        )


# Rendered `<option>` markup, keyed on the option template and the choice list. Both are fixed
# for a field whose choices are a module-level constant, so an entry is built once per process
# and reused for the life of it. Each entry holds the unselected and selected rendering of every
# option, both produced by the option template itself rather than by formatting a string here,
# which is what makes the output byte-identical by construction instead of by inspection.
_CACHED_OPTION_HTML = {}


class CachedStaticOptionsMixin:
    """Render a static choice list's `<option>` elements once per process instead of per request.

    Django's `select.html` includes the option template once per option, and Nautobot's
    `selectwithdisabled_option.html` then includes `attrs.html` itself, so every option costs two
    template renders. On a field like `PrefixFilterForm.prefix_length`, whose 130 choices come
    from a module-level constant, that is ~25ms of a ~143ms page spent re-rendering identical
    markup into a drawer that is closed until the user opens it.

    The rendered markup is a pure function of `(value, label, selected)`: the option template
    reads only those, `option_inherits_attrs` is False so options never carry the select's
    attributes, and `selected` is the only per-option attribute Django ever sets. So the two
    possible renderings of each option can be cached and chosen between.

    Building the option dictionaries is left to Django. `optgroups()` costs 0.2ms against the
    25ms of template rendering, so reimplementing selection semantics would buy under 1% and
    risk diverging from them.

    The fast path declines, falling back to the normal one, when anything it does not model is
    present: named option groups, a choice list that is not a plain sequence, or an option
    carrying any attribute other than `selected`.
    """

    cached_options_template_name = "widgets/cached_options_select.html"

    def render(self, name, value, attrs=None, renderer=None):
        context = self.get_context(name, value, attrs)
        options = self._cached_options_html(name, context["widget"]["optgroups"], renderer)
        if options is None:
            return self._render(self.template_name, context, renderer)
        context["widget"]["cached_options"] = options
        return self._render(self.cached_options_template_name, context, renderer)

    def _cached_options_html(self, name, optgroups, renderer):
        """Joined `<option>` markup for these optgroups, or None to use the normal path."""
        if not isinstance(self.choices, (list, tuple)):
            return None
        flat = []
        key_parts = []
        for group_name, group_options, _ in optgroups:
            if group_name:
                return None
            for option in group_options:
                if set(option["attrs"]) - {"selected"}:
                    return None
                flat.append(option)
                key_parts.append((str(option["value"]), str(option["label"])))

        # `name` is in the key because an option template is free to read `widget.name`, and two
        # fields can share a choice list under different names. Every option template in core
        # reads only value, label and attrs, so this is insurance against an App's template
        # rather than against anything shipped here. `index` needs no such care: the cache is a
        # list, and the variant at position i was rendered from the option whose index is i.
        key = (self.option_template_name, name, tuple(key_parts))
        variants = _CACHED_OPTION_HTML.get(key)
        if variants is None:
            variants = self._build_option_variants(flat, renderer)
            _CACHED_OPTION_HTML[key] = variants

        # `select.html` emits a newline and two spaces before each option; reproduced here
        # because this replaces that loop rather than the template around it.
        return mark_safe(
            "".join(f"\n  {variants[i][bool(option['selected'])]}" for i, option in enumerate(flat))
        )

    def _build_option_variants(self, flat, renderer):
        """Both renderings of every option, produced by the option template itself.

        Rendered through the renderer's own template rather than through `Widget._render`,
        because `_render` calls the renderer, and the renderer calls `.strip()` on what it
        returns. `select.html` reaches the option template through `{% include %}`, which does
        not strip, so the markup it produces keeps the trailing newline the file ends with.
        Going through `_render` drops that newline and the joined output is one byte per option
        short of what Django emits. Django's form templates live in the renderer's engine rather
        than the project's, so `django.template.loader.get_template` cannot find them at all.
        """
        engine = renderer or get_default_renderer()
        variants = []
        for option in flat:
            pair = []
            for selected in (False, True):
                one = dict(option)
                one["attrs"] = dict(option["attrs"])
                one["selected"] = selected
                if selected:
                    one["attrs"]["selected"] = True
                else:
                    one["attrs"].pop("selected", None)
                pair.append(engine.get_template(one["template_name"]).render({"widget": one}))
            variants.append(tuple(pair))
        return variants


class SelectWithDisabled(CachedStaticOptionsMixin, forms.Select):
    """
    Modified the stock Select widget to accept choices using a dict() for a label. The dict for each option must include
    'label' (string) and 'disabled' (boolean).
    """

    option_template_name = "widgets/selectwithdisabled_option.html"


class StaticSelect2(SelectWithDisabled):
    """
    A static <select> form widget using the Select2 library.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.attrs["class"] = "nautobot-select2-static"


class StaticSelect2Multiple(StaticSelect2, forms.SelectMultiple):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.attrs["data-multiple"] = 1


class SelectWithPK(StaticSelect2):
    """
    Include the primary key of each option in the option label (e.g. "Router7 (4721)").
    """

    option_template_name = "widgets/select_option_with_pk.html"


class ContentTypeSelect(StaticSelect2):
    """
    Appends an `api-value` attribute equal to the slugified model name for each ContentType. For example:
        <option value="37" api-value="console-server-port">console server port</option>
    This attribute can be used to reference the relevant API endpoint for a particular ContentType.
    """

    option_template_name = "widgets/select_contenttype.html"


class MinimalModelChoiceIterator(ModelChoiceIterator):
    """
    Helper class for APISelect and APISelectMultiple.

    Allows the widget to keep a full `queryset` for data validation, but, for performance reasons, returns a minimal
    subset of choices at render time derived from the widget's `data_queryset`.
    """

    @property
    def queryset(self):
        return self.field.data_queryset

    @queryset.setter
    def queryset(self, value):
        return self.field.data_queryset


class APISelect(SelectWithDisabled):
    """
    A select widget populated via an API call

    Args:
        api_url (str): API endpoint URL. Required if not set automatically by the parent field.
        api_version (str): API version.
    """

    def __init__(self, api_url=None, full=False, api_version=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.attrs["class"] = "nautobot-select2-api"

        if api_version:
            # Set Request Accept Header api-version e.g Accept: application/json; version=1.2
            self.attrs["data-api-version"] = api_version

        if api_url:
            # Prefix the URL w/ the script prefix (e.g. `/nautobot`)
            self.attrs["data-url"] = urljoin(get_script_prefix(), api_url.lstrip("/"))

    def add_query_param(self, name, value):
        """
        Add details for an additional query param in the form of a data-* JSON-encoded list attribute.

        Args:
            name (str): The name of the query param
            value (Any): The value of the query param
        """
        key = f"data-query-param-{name}"

        values = json.loads(self.attrs.get(key, "[]"))
        if isinstance(value, (list, tuple)):
            values.extend([str(v) for v in value])
        else:
            values.append(str(value))

        self.attrs[key] = json.dumps(values, ensure_ascii=False)

    def get_context(self, name, value, attrs):
        # This adds null options to DynamicModelMultipleChoiceField selected choices
        # example <select ..>
        #           <option .. selected value="null">None</option>
        #           <option .. selected value="1234-455...">Rack 001</option>
        #           <option .. value="1234-455...">Rack 002</option>
        #          </select>
        # Prepend null choice to self.choices if
        # 1. form field allow null_option e.g. DynamicModelMultipleChoiceField(..., null_option="None"..)
        # 2. if null is part of url query parameter for name(field_name) i.e. http://.../?rack_id=null
        # 3. if both value and choices are iterable
        if (
            self.attrs.get("data-null-option")
            and isinstance(value, (list, tuple))
            and "null" in value
            and isinstance(self.choices, Iterable)
        ):

            class ModelChoiceIteratorWithNullOption(MinimalModelChoiceIterator):
                def __init__(self, *args, **kwargs):
                    self.null_options = kwargs.pop("null_option", None)
                    super().__init__(*args, **kwargs)

                def __iter__(self):
                    # ModelChoiceIterator.__iter__() yields a tuple of (value, label)
                    # using this approach first yield a tuple of (null(value), null_option(label))
                    yield "null", self.null_options
                    yield from super().__iter__()

            null_option = self.attrs.get("data-null-option")
            self.choices = ModelChoiceIteratorWithNullOption(field=self.choices.field, null_option=null_option)

        return super().get_context(name, value, attrs)


class APISelectMultiple(APISelect, forms.SelectMultiple):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.attrs["data-multiple"] = 1


class DatePicker(forms.TextInput):
    """
    Date picker using Flatpickr.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs["class"] = "date-picker"
        self.attrs["placeholder"] = "YYYY-MM-DD"


class DateTimePicker(forms.TextInput):
    """
    DateTime picker using Flatpickr.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs["class"] = "datetime-picker"
        self.attrs["placeholder"] = "YYYY-MM-DD hh:mm:ss"


class TimePicker(forms.TextInput):
    """
    Time picker using Flatpickr.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs["class"] = "time-picker"
        self.attrs["placeholder"] = "hh:mm:ss"


class MultiValueCharInput(StaticSelect2Multiple):
    """
    Manual text input with tagging enabled.
    Press enter to create a new entry.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs["class"] = "nautobot-select2-multi-value-char"


class ClearableFileInput(forms.ClearableFileInput):
    template_name = "widgets/clearable_file.html"


class NumberWithSelect(forms.NumberInput):
    template_name = "widgets/number_input_with_choices.html"

    def __init__(self, choices=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if choices is None:
            self.choices = []
        elif hasattr(choices, "CHOICES"):
            self.choices = core_choices.unpack_grouped_choices(choices.CHOICES)
        else:
            self.choices = core_choices.unpack_grouped_choices(choices)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["choices"] = self.choices
        return context
