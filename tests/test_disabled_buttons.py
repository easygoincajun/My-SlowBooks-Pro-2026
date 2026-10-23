"""A disabled button looks disabled (macbase1 NEW-15, 2.18.0 gate). The
stylesheet had no disabled style for .btn: a disabled button drew at full
colour with a pointer cursor, and a click did nothing and said nothing;
Restore's "Replace my books" was like that before its tick."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_a_disabled_button_is_dimmed_with_no_hover():
    css = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
    rule = re.search(r"\.btn:disabled\s*\{([^}]*)\}", css)
    assert rule and "opacity: 0.55" in rule.group(1)
    assert "cursor: not-allowed" in rule.group(1)
    for sel in (".btn", ".btn-primary", ".btn-secondary", ".btn-danger"):
        assert f"\n{sel}:hover {{" not in css, sel
        assert f"\n{sel}:hover:not(:disabled) {{" in css, sel
