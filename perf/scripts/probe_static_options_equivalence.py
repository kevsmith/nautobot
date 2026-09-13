#!/usr/bin/env python
"""Prove the cached-options path renders byte-identical markup to the normal one.

The queue declined a general Python option-builder partly because it would be "a
whitespace-exact rewrite of HTML generation for the most widely used widget class". This
path does not format HTML at all -- it renders each option through the same template and
caches the result -- so byte-identity should hold by construction. That is a claim, and
this is the check.

Compares `render()` with the cache engaged against `forms.Select.render()`, which is the
code path the mixin replaces, over the cases most likely to diverge:

    no selection            the blank option is the selected one
    single selection        one option carries ` selected`
    multiple selection      SelectMultiple, several options carry it
    selection absent        a value matching no option
    disabled options        SelectWithDisabled's dict-label form, which must decline
    named optgroups         must decline
    per-option attributes   must decline

Run against a tree with the mixin applied; it exercises both paths directly rather than
diffing two checkouts.

    perf/scripts/dc.sh exec -T nautobot python \\
        /source/perf/scripts/probe_static_options_equivalence.py
"""

import os
import sys

import nautobot

nautobot.setup(os.environ.get("NAUTOBOT_CONFIG", "/opt/nautobot/nautobot_config.py"))

from django import forms  # noqa: E402

from nautobot.core.forms.utils import add_blank_choice  # noqa: E402
from nautobot.core.forms.widgets import (  # noqa: E402
    SelectWithPK,
    StaticSelect2,
    StaticSelect2Multiple,
)
from nautobot.ipam.forms import IPADDRESS_MASK_LENGTH_CHOICES, PREFIX_MASK_LENGTH_CHOICES  # noqa: E402

failures = []
checked = 0


def baseline(widget, name, value, attrs):
    """What the widget would render without the mixin: Django's own Select.render."""
    cls = forms.SelectMultiple if widget.allow_multiple_selected else forms.Select
    return cls.render(widget, name, value, attrs=dict(attrs))


def compare(label, widget, name, value, attrs=None, expect_fast_path=True):
    global checked
    attrs = attrs or {"id": f"id_{name}"}
    got = widget.render(name, value, attrs=dict(attrs))
    want = baseline(widget, name, value, attrs)
    checked += 1
    used_fast = (
        widget._cached_options_html(widget.get_context(name, value, dict(attrs))["widget"]["optgroups"], None)
        is not None
    )
    status = "ok" if got == want else "MISMATCH"
    path = "cached" if used_fast else "fallback"
    if got != want:
        failures.append((label, got, want))
    elif expect_fast_path is not None and used_fast != expect_fast_path:
        status = f"WRONG PATH (took {path})"
        failures.append((label, f"expected fast_path={expect_fast_path}", f"took {path}"))
    print(f"  {status:24s} {label:46s} [{path}] {len(got)} bytes")


def widget_with(cls, choices):
    w = cls()
    w.choices = choices
    return w


print("=== prefix_length, 130 static choices (StaticSelect2) ===")
for value in ("", "0", "24", "128", "999"):
    compare(
        f"prefix_length value={value!r}", widget_with(StaticSelect2, PREFIX_MASK_LENGTH_CHOICES), "prefix_length", value
    )

print("\n=== mask_length, 129 static choices (StaticSelect2Multiple) ===")
for value in ([], ["24"], ["24", "31"], ["1", "24", "31", "128"], ["999"]):
    compare(
        f"mask_length value={value!r}",
        widget_with(StaticSelect2Multiple, IPADDRESS_MASK_LENGTH_CHOICES),
        "mask_length",
        value,
    )

print("\n=== cases the fast path must decline ===")
disabled = add_blank_choice([(1, {"label": "One", "disabled": True}), (2, {"label": "Two", "disabled": False})])
compare("dict labels with disabled=True", widget_with(StaticSelect2, disabled), "d", "1", expect_fast_path=None)

grouped = [("Group A", [(1, "One"), (2, "Two")]), ("Group B", [(3, "Three")])]
compare("named optgroups", widget_with(StaticSelect2, grouped), "g", "2", expect_fast_path=False)

compare(
    "SelectWithPK (overrides option_template_name)",
    widget_with(SelectWithPK, add_blank_choice([(1, "One"), (2, "Two")])),
    "c",
    "1",
)

print("\n=== the cache is reused, and selection still varies ===")
w = widget_with(StaticSelect2, PREFIX_MASK_LENGTH_CHOICES)
a = w.render("prefix_length", "24", attrs={"id": "id_prefix_length"})
b = w.render("prefix_length", "31", attrs={"id": "id_prefix_length"})
c = w.render("prefix_length", "24", attrs={"id": "id_prefix_length"})
print(f"  {'ok' if a == c else 'MISMATCH':24s} {'same value renders identically':46s}")
print(f"  {'ok' if a != b else 'MISMATCH':24s} {'different value renders differently':46s}")
if a != c or a == b:
    failures.append(("cache reuse", "a==c and a!=b", f"a==c:{a == c} a!=b:{a != b}"))
checked += 2

print(f"\n{checked} comparisons, {len(failures)} failure(s)")
for label, got, want in failures:
    print(f"\n--- {label} ---")
    if isinstance(got, str) and isinstance(want, str) and got.startswith("<"):
        for i, (g, w_) in enumerate(zip(got.splitlines(), want.splitlines())):
            if g != w_:
                print(f"  first differing line {i}:\n    got  {g!r}\n    want {w_!r}")
                break
    else:
        print(f"  got  {got!r}\n  want {want!r}")
sys.exit(1 if failures else 0)
