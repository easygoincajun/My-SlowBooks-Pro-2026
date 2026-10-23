"""The Interoperability page (QuickBooks IIF) says when an import came back with errors
(#197): both imports end through IIFPage._reportImport, the one place that
says "Import complete". The page itself is driven in a browser in
test_browser_ui.py; this needs none."""

from pathlib import Path

JS = (
    Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "iif.js"
).read_text(encoding="utf-8")


def test_both_imports_report_through_the_one_helper():
    # the IIF import also passes what it could not take (#288); the report
    # CSV import has no such count
    assert JS.count("IIFPage._reportImport(total, (result.errors || []).length") == 2


def test_only_the_helper_says_import_complete():
    assert JS.count("App.setStatus('Interoperability — Import complete')") == 1
    helper = JS.split(
        "_reportImport(total, errors, unsupported = 0, words = null) {", 1
    )[1].split("\n    },", 1)[0]
    assert "Import complete" in helper and "if (errors > 0)" in helper
