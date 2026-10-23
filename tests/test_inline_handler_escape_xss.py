"""No inline handler embeds an escaped value inside a JS string literal.

`reseller_permits.js` built:

    onclick="ResellerPermitsPage._copy('${escapeHtml(value)}', 'Label')"

for the permit number, the business name and the tax ID — all
operator-entered text. `escapeHtml()` turns a single quote into `&#39;`, and
the HTML parser decodes `&#39;` back to `'` BEFORE the JS engine ever sees the
attribute. So the escaped value lands inside the decoded handler as a live
quote: `Joe's Pizza` broke the button, and `x');alert(1);('` closed the string
and ran script (stored XSS). HTML-escaping is for HTML text and attribute
context; it is the wrong tool for a value sitting inside a JS string literal.

`test_onclick_attributes_parse.py` once called this exact shape — single
quotes inside the double-quoted attribute, value escaped — "the established
safe pattern." It is not. The safe pattern already used elsewhere in the app
(employees.js, companies.js, app.js) puts the value in a `data-` attribute and
reads it back through `this.dataset` in the handler: a `data-` attribute is
decoded to text the parser never treats as code.

This guard refuses the whole class: a single-quoted JS string whose body is an
`${... escapeHtml(...) ...}` interpolation, anywhere in app/static/js. The
fixed reseller buttons no longer match. A short allowlist records the call
sites audited and judged safe because their input cannot carry a quote; each
carries its reason, and anything new fails until it is fixed or justified.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "app/static/js"
JS_FILES = sorted(p for p in JS_DIR.glob("*.js") if p.name != "chart.umd.js")

# A single-quoted JS string whose entire body is one interpolation that calls
# escapeHtml. That is only ever wrong: escapeHtml emits &#39;/&quot;, which the
# HTML parser decodes before the JS string is parsed, so the value can break
# out. (A value belongs in a data- attribute, read via this.dataset.)
_DANGER = re.compile(r"'\$\{[^}]*escapeHtml\([^}]*\}'")

# Call sites audited and judged safe because the interpolated value is
# structurally incapable of carrying a quote. Keyed by (file, fragment) so
# the exemption is exact — any other occurrence, or any change to these, fails.
# These stay as they are: the fix is the reseller dialog; converting these
# too would be defence in depth, not a fix.
_AUDITED_SAFE = {
    # username is constrained server-side by _USERNAME_RE =
    # ^[a-z0-9][a-z0-9._-]{2,49}$ (app/routes/users.py) — no quote, space,
    # paren or semicolon is accepted at create or update time.
    ("settings.js", "'${escapeHtml(u.username)}'"),
    # source_type is a Transaction enum the app sets ('invoice', 'bill',
    # 'payment', ...), never operator free text.
    ("jobs.js", "'${escapeHtml(l.source_type || '')}'"),
    # href is a server-built register path (bank_register.source_link, from
    # fixed _LINKS patterns + numeric ids) or `#/journal/<id>` — no user text.
    ("classes.js", "'${escapeHtml(href.replace(/^\\//, ''))}'"),
    # target is a developer-authored anchor/selector (form data-skip-to:
    # '#je-lines', '#bill-lines', ...), never user input.
    ("utils.js", "'${escapeHtml(target)}'"),
}


def test_no_inline_handler_embeds_an_escaped_value_in_a_js_string():
    offenders = []
    for path in JS_FILES:
        src = path.read_text(encoding="utf-8")
        for fragment in _DANGER.findall(src):
            if (path.name, fragment) not in _AUDITED_SAFE:
                offenders.append(f"{path.name}: {fragment}")
    assert not offenders, (
        "An escaped value sits inside a single-quoted JS string in an inline "
        "handler. The HTML parser decodes the escape before the JS runs, so a "
        "value with a quote breaks out. Carry it in a data- "
        "attribute read via this.dataset, or justify it in _AUDITED_SAFE.\n"
        + "\n".join(offenders)
    )


def test_the_reseller_copy_buttons_use_the_data_attribute_pattern():
    """The specific fix, named: the reseller Copy buttons must not reappear as
    inline-string handlers, and must read their text from the dataset."""
    src = (JS_DIR / "reseller_permits.js").read_text(encoding="utf-8")
    assert not _DANGER.search(
        src
    ), "a reseller Copy button went back to an inline-string handler"
    assert (
        src.count('data-copy="${escapeHtml(') == 3
    ), "expected three data-copy Copy buttons"
    assert "ResellerPermitsPage._copy(this.dataset.copy, this.dataset.copyLabel)" in src


def test_the_control_fragment_is_detected():
    """The matcher really fires on the retired shape, so a green suite means
    the pattern is absent, not that the regex is inert."""
    assert _DANGER.search(
        """onclick="X._copy('${escapeHtml(permit.permit_number)}', 'Permit #')" """
    )
