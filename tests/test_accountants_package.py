"""The Accountant's Package (#343): one zip for a period, saved like every
other download, each file the same report run on its own, with a README
that lists what is inside."""

import io
import zipfile
from pathlib import Path

from tests.conftest import WEASYPRINT_AVAILABLE

ROOT = Path(__file__).resolve().parents[1]
QS = "start_date=2026-01-01&end_date=2026-12-31"


def _journal(client, accounts, day, amount, text, **extra):
    r = client.post(
        "/api/journal",
        json={
            "date": day,
            "description": text,
            "lines": [
                {"account_id": accounts["6300"].id, "debit": amount, "credit": "0"},
                {"account_id": accounts["1000"].id, "debit": "0", "credit": amount},
            ],
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _zip(client, headers=None):
    r = client.get(f"/api/reports/accountants-package?{QS}", headers=headers or {})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    return r, zipfile.ZipFile(io.BytesIO(r.content))


def test_one_zip_holds_every_report_and_each_equals_the_standalone_one(
    client, seed_accounts
):
    _journal(client, seed_accounts, "2026-02-10", "40.00", "Insurance paid")
    _journal(
        client, seed_accounts, "2026-03-31", "100.00", "Accrual", is_adjusting=True
    )
    client.post("/api/customers", json={"name": "Salt & Pine Catering"})

    r, z = _zip(client)
    names = z.namelist()
    assert names[0] == "README.txt"
    expected = {
        "general-ledger_2026-01-01_2026-12-31.csv",
        "trial-balance_2026-01-01_2026-12-31.csv",
        "ar-aging_2026-12-31.csv",
        "ap-aging_2026-12-31.csv",
        "adjusting-entries_2026-01-01_2026-12-31.csv",
        "closing-date-exceptions_2026-01-01_2026-12-31.csv",
        "audit-log_2026-01-01_2026-12-31.csv",
    }
    assert expected <= set(names), names
    if WEASYPRINT_AVAILABLE:
        assert "financial-statements_2026-01-01_2026-12-31.pdf" in names
        assert z.read("financial-statements_2026-01-01_2026-12-31.pdf")[:4] == b"%PDF"
    assert (
        "attachment; filename=accountants-package_" in r.headers["content-disposition"]
    )
    assert r.headers["content-disposition"].endswith("_2026-01-01_2026-12-31.zip")

    # each CSV is the standalone report, byte for byte
    def inside(name):
        return z.read(name).decode("utf-8-sig")

    assert inside("trial-balance_2026-01-01_2026-12-31.csv") == client.get(
        f"/api/reports/trial-balance/csv?{QS}"
    ).text.lstrip("﻿")
    assert inside("general-ledger_2026-01-01_2026-12-31.csv") == client.get(
        f"/api/reports/general-ledger/csv?{QS}"
    ).text.lstrip("﻿")
    assert inside("adjusting-entries_2026-01-01_2026-12-31.csv") == client.get(
        f"/api/reports/adjusting-entries/csv?{QS}"
    ).text.lstrip("﻿")
    assert inside("closing-date-exceptions_2026-01-01_2026-12-31.csv") == client.get(
        f"/api/reports/closing-date-exceptions/csv?{QS}"
    ).text.lstrip("﻿")
    # the agings carry their totals; the audit log names the changes
    assert "Accounts Receivable Aging" in inside("ar-aging_2026-12-31.csv")
    assert inside("ar-aging_2026-12-31.csv").splitlines()[-1].startswith("Total,")
    audit = inside("audit-log_2026-01-01_2026-12-31.csv")
    assert "customer 1 (Salt & Pine Catering)" in audit
    assert "journal entry" in audit and "admin" in audit

    readme = inside("README.txt")
    assert (
        "Accountant's Package" in readme and "Period 2026-01-01 to 2026-12-31" in readme
    )
    for name in names[1:]:
        assert f"  {name}: " in readme, name
    assert "No bank reconciliation was completed in the period." in readme


def test_the_audit_log_starts_at_the_closing_date_when_one_is_set(
    client, seed_accounts
):
    r = client.put("/api/settings", json={"closing_date": "2026-06-30"})
    assert r.status_code == 200, r.text
    _journal(client, seed_accounts, "2026-09-01", "5.00", "After")
    _, z = _zip(client)
    assert "audit-log_2026-06-30_2026-12-31.csv" in z.namelist()
    readme = z.read("README.txt").decode("utf-8")
    assert "Closing date in force: 2026-06-30." in readme
    assert "(the closing date)" in readme


def test_the_manifest_lists_the_files_and_the_desktop_gets_it_inline(
    client, seed_accounts
):
    m = client.get(f"/api/reports/accountants-package/manifest?{QS}").json()
    assert m["files"][0] == "README.txt"
    assert "trial-balance_2026-01-01_2026-12-31.csv" in m["files"]
    assert m["start_date"] == "2026-01-01" and m["end_date"] == "2026-12-31"
    r, _ = _zip(client, headers={"X-Slowbooks-Desktop": "1"})
    assert r.headers["content-disposition"].startswith("inline; filename=")


def test_the_page_offers_the_package():
    reports_js = (ROOT / "app/static/js/reports.js").read_text(encoding="utf-8")
    assert "'accountants-package':" in reports_js
    assert "card('accountants-package', \"Accountant's Package\"" in reports_js
    assert "/reports/accountants-package/manifest?" in reports_js
    assert (
        "window.open('/api/reports/accountants-package?${qs}','_blank')" in reports_js
    )
