"""Open screens (#336): the sources. The sidebar lists the session's open
screens from sessionStorage; a page lists when it renders, a dialog
report or document when its address is set, and the list redraws when a
dialog closes. The browser run is tests/test_browser_open_tabs.py."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_hooks_and_the_list_are_in_place():
    src = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    # kept for the session, by address
    assert "OPEN_TABS_KEY: 'slowbooks-open-screens'" in src
    assert "sessionStorage.getItem(App.OPEN_TABS_KEY)" in src
    # a page shown, a dialog addressed, a dialog closed
    assert "App.screenShown(route);" in src
    assert "App.screenShown(null);" in src
    assert src.count("App.renderOpenTabs()") >= 3
    # the Dashboard and the Report Center are not listed
    assert "if (key === '/' || key === '/reports')" in src
    # each row is a link back and a close button with the screen's name
    assert 'class="nav-link open-screens__link' in src
    assert 'aria-label="Close ${escapeHtml(t.label)}"' in src
    assert "closeAllTabs()" in src
    # a report lists with its period
    for words in ("this year to date", "last quarter", "as of ${formatDate("):
        assert words in src


def test_the_styles_cover_both_themes():
    light = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
    dark = (ROOT / "app/static/css/dark.css").read_text(encoding="utf-8")
    assert ".open-screens__close" in light and ".open-screens__clear" in light
    assert ".open-screens__close" in dark


def test_the_feature_is_documented():
    features = (ROOT / "docs/features.md").read_text(encoding="utf-8")
    assert "Open screens" in features or "open screens" in features
