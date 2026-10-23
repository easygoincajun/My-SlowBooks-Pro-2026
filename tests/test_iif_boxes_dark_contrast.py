"""The IIF page's message boxes read at AA in the dark theme (2.23.0 gate,
W-5 / NEW-88): the three boxes kept light backgrounds under the theme's
text colours, so #288's warning read at 2.75:1 and the file name at
1.96:1. dark.css gives each its own ground; this reads the literal colours
back and holds every pair to 4.5:1.
"""

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "app/static/css/dark.css").read_text(
    encoding="utf-8"
)


def _rule(selector):
    m = re.search(re.escape(f'[data-theme="dark"] {selector} {{') + r"([^}]*)}", CSS)
    assert m, selector
    return dict(
        (k.strip(), v.strip())
        for k, v in (d.split(":", 1) for d in m.group(1).split(";") if ":" in d)
    )


def _lum(hex_color):
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def ratio(fg, bg):
    a, b = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def test_the_error_and_warning_boxes_read_at_aa_in_the_dark_theme():
    for box in (".iif-errors", ".iif-warnings"):
        rule = _rule(box)
        assert ratio(rule["color"], rule["background"]) >= 4.5, box


def test_the_file_info_box_reads_at_aa_in_the_dark_theme():
    bg = _rule(".iif-file-info")["background"]
    for part in (".iif-file-info .filename", ".iif-file-info .filesize"):
        assert ratio(_rule(part)["color"], bg) >= 4.5, part


def test_the_report_import_puts_a_warning_in_the_warnings_box():
    # #291's Open Balance notice sat in the error box with an orange border
    js = (Path(__file__).resolve().parents[1] / "app/static/js/iif.js").read_text(
        encoding="utf-8"
    )
    assert "border-color:var(--warning" not in js
    assert js.count('<div class="iif-warnings">') >= 3
