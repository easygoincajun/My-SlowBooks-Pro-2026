"""Open screens (#336), in playwright's Chromium on the bakery's books: the
pages, registers and dialog reports opened in a session are listed under
"Open" at the top of the sidebar, each a link back and each closable; the
one showing is marked; a report lists with its period; closing the screen
showing goes to the one before it; a reload keeps the list; Close all
empties it. Skipped where playwright or its Chromium is not installed
(tests/test_open_tabs.py checks the sources)."""

import pytest

pytest.importorskip("playwright.sync_api")

from tests.test_browser_report_views import _hash, _open_at  # noqa: E402
from tests.test_theme_contrast import (  # noqa: E402,F401  (the fixtures)
    _visit,
    browser_fixture,
    company_fixture,
    settle,
)

MODAL_SHOWN = (
    "() => !document.getElementById('modal-overlay').classList.contains('hidden')"
)
TITLE = "() => document.getElementById('modal-title').textContent"
TABS = """() => [...document.querySelectorAll('#open-screens .open-screens__link')]
    .map(a => ({ label: a.textContent.trim(), key: a.dataset.key, here: a.getAttribute('aria-current') === 'page' }))"""
GROUP = "() => !!document.getElementById('open-screens')"


def _ok(r):
    assert r.status_code in (200, 201), (str(r.request.url), r.status_code, r.text)
    return r.json()


def _tabs(page):
    return page.evaluate(TABS)


def test_the_screens_opened_are_listed_linked_marked_and_closable(
    browser, company, books
):
    accounts = {a["account_number"]: a for a in _ok(company.get("/api/accounts"))}
    checking = accounts["1000"]["id"]
    page, handled = _open_at(browser, company, "#/banking")
    try:
        # the Banking page is the first screen; the Dashboard is never listed
        assert _tabs(page) == [{"label": "Banking", "key": "/banking", "here": True}]
        # the register: named for its account, the page's own heading
        _visit(page, handled, f"#/banking/{checking}")
        tabs = _tabs(page)
        assert [t["label"] for t in tabs] == ["Banking", "Checking"]
        assert [t["here"] for t in tabs] == [False, True]
        # a dialog report, with its period
        _visit(page, handled, "#/reports/profit-loss?period=last_month")
        page.wait_for_selector("#report-content table")
        assert page.evaluate(MODAL_SHOWN)
        tabs = _tabs(page)
        assert [t["label"] for t in tabs] == [
            "Banking",
            "Checking",
            "Profit & Loss · last month",
        ]
        assert tabs[2]["here"] and tabs[2]["key"] == _hash(page).lstrip("#")
        # the same report on another period is the same screen, renamed
        page.select_option("#report-period-select", "this_year_to_date")
        page.wait_for_function("() => /this_year_to_date/.test(location.hash)")
        settle(page, handled)
        labels = [t["label"] for t in _tabs(page)]
        assert labels == ["Banking", "Checking", "Profit & Loss · this year to date"]
        # the Report Center it opened over is not listed as a screen of its own
        assert "Reports" not in labels

        # a link back, clicked while the report is showing: a view's overlay
        # leaves the sidebar usable (W-22 / NEW-159), so the register page
        # opens, the dialog goes and the mark moves
        assert page.evaluate(
            "() => document.body.classList.contains('dialog-addressed')"
        )
        hit = page.evaluate(
            """() => { const a = document.querySelector('#open-screens .open-screens__link[data-key="/banking/%d"]');
                       const r = a.getBoundingClientRect();
                       return document.elementFromPoint(r.left + 4, r.top + r.height / 2) === a; }"""
            % checking
        )
        assert hit, "the Open link is under the overlay"
        page.click(
            '#open-screens .open-screens__link[data-key="/banking/%d"]' % checking
        )
        settle(page, handled)
        assert _hash(page) == f"#/banking/{checking}"
        assert not page.evaluate(MODAL_SHOWN)
        assert not page.evaluate(
            "() => document.body.classList.contains('dialog-addressed')"
        )
        tabs = _tabs(page)
        assert len(tabs) == 3 and [t["here"] for t in tabs] == [False, True, False]

        # × on another screen takes it off the list; the page stays
        page.click('#open-screens .open-screens__close[data-key="/banking"]')
        tabs = _tabs(page)
        assert [t["label"] for t in tabs][0] == "Checking" and len(tabs) == 2
        assert _hash(page) == f"#/banking/{checking}"

        # × on the screen showing goes to the one before it: here the first
        # in the list is showing, so the next one (the report) opens
        page.click(
            '#open-screens .open-screens__close[data-key="/banking/%d"]' % checking
        )
        page.wait_for_function("() => /profit-loss/.test(location.hash)")
        settle(page, handled)
        page.wait_for_selector("#report-content table")
        assert page.evaluate(TITLE) == "Profit & Loss"
        tabs = _tabs(page)
        assert len(tabs) == 1 and tabs[0]["here"]

        # a reload keeps the session's list
        page.reload()
        page.wait_for_function("window.App && document.readyState === 'complete'")
        page.evaluate(
            "() => { const s = document.getElementById('splash'); if (s) s.classList.add('hidden'); }"
        )
        settle(page, handled)
        page.wait_for_selector("#report-content table")
        assert len(_tabs(page)) == 1
        # a second screen brings Close all
        _visit(page, handled, "#/customers")
        assert len(_tabs(page)) == 2

        # Close all
        page.click("#open-screens .open-screens__clear")
        assert not page.evaluate(GROUP)
        stored = page.evaluate(
            "() => JSON.parse(sessionStorage.getItem('slowbooks-open-screens'))"
        )
        assert stored["tabs"] == [] and stored["owner"].startswith("admin|")
    finally:
        page.close()


def test_a_document_lists_with_its_title(browser, company, books):
    inv = _ok(company.get("/api/invoices"))[0]
    page, handled = _open_at(browser, company, f"#/invoices/{inv['id']}")
    try:
        page.wait_for_function(MODAL_SHOWN)
        settle(page, handled)
        tabs = _tabs(page)
        assert len(tabs) == 1 and tabs[0]["key"] == f"/invoices/{inv['id']}"
        assert tabs[0]["label"] == page.evaluate(TITLE).strip()
        assert tabs[0]["here"]
        # View on the list opens the same document by its address (NEW-164)
        page.keyboard.press("Escape")
        page.wait_for_function("() => !(%s)()" % MODAL_SHOWN)
        page.evaluate("async (id) => { await InvoicesPage.view(id); }", inv["id"])
        settle(page, handled)
        assert _hash(page) == f"#/invoices/{inv['id']}"
        tabs = _tabs(page)
        assert len(tabs) == 1 and tabs[0]["here"]
    finally:
        page.close()


def test_the_list_belongs_to_one_sign_in(browser, company, books):
    """Another sign-in on the same window starts with its own list (NEW-164)."""
    page, handled = _open_at(browser, company, "#/customers")
    try:
        assert len(_tabs(page)) == 1
        # a stored list of another owner is dropped once the sign-in is known
        page.evaluate(
            """() => { const s = JSON.parse(sessionStorage.getItem('slowbooks-open-screens'));
                       s.owner = 'someone-else|Another Co'; s.tabs.push({ key: '/vendors', label: 'Vendors', n: 1, load: 'old' });
                       sessionStorage.setItem('slowbooks-open-screens', JSON.stringify(s)); }"""
        )
        page.reload()
        page.wait_for_function("window.App && document.readyState === 'complete'")
        page.evaluate(
            "() => { const s = document.getElementById('splash'); if (s) s.classList.add('hidden'); }"
        )
        settle(page, handled)
        tabs = _tabs(page)
        assert [t["key"] for t in tabs] == ["/customers"]
    finally:
        page.close()
