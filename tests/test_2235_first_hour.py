"""A bookkeeper's first hour (2.23.5): the drill-down saves as CSV and PDF
(#333); the account number is plainly optional and the seeded chart's
unused accounts go in one step (#332); the vendor list has a search box and
a vendor's page shows what was paid to date, with a CSV (#335)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.models.accounts import Account, AccountType
from app.models.bills import BillPayment
from app.models.contacts import Vendor
from app.models.transactions import TransactionLine
from app.services.accounting import create_journal_entry
from tests.conftest import WEASYPRINT_AVAILABLE

ROOT = Path(__file__).resolve().parents[1]
PERIOD = {"start_date": "2026-01-01", "end_date": "2026-12-31"}


def _post_expense(db, seed, amount="40.00", day=date(2026, 3, 1)):
    lines = [
        {
            "account_id": seed["6000"].id,
            "debit": Decimal(amount),
            "credit": Decimal("0"),
            "description": "Flyers",
        },
        {
            "account_id": seed["1000"].id,
            "debit": Decimal("0"),
            "credit": Decimal(amount),
            "description": "Flyers",
        },
    ]
    create_journal_entry(db, day, "Print shop", lines)
    db.commit()


# ---------------------------------------------------------------- #333


def test_the_drill_down_saves_as_csv_with_the_figures_on_the_screen(
    client, db_session, seed_accounts
):
    _post_expense(db_session, seed_accounts)
    acct = seed_accounts["6000"].id
    screen = client.get(
        "/api/reports/account-transactions", params={"account_id": acct, **PERIOD}
    ).json()
    assert screen["entries"] and screen["period_debit"] == 40.0
    r = client.get(
        "/api/reports/account-transactions/csv", params={"account_id": acct, **PERIOD}
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "AccountDetail_6000-advertising" in r.headers["content-disposition"]
    text = r.text
    assert "Account detail" in text and "Balance brought forward" in text
    assert "2026-03-01" in text and "Print shop" in text and "40.00" in text
    assert "Period total" in text
    js = (ROOT / "app/static/js/reports.js").read_text(encoding="utf-8")
    assert "_exportButtons('account-transactions', exportQs)" in js


def test_the_drill_down_saves_as_pdf(client, db_session, seed_accounts):
    _post_expense(db_session, seed_accounts)
    if not WEASYPRINT_AVAILABLE:
        return
    r = client.get(
        "/api/reports/account-transactions/pdf",
        params={"account_id": seed_accounts["6000"].id, **PERIOD},
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "application/pdf"
    )


# ---------------------------------------------------------------- #332


def test_the_account_form_says_the_number_is_optional(
    client, db_session, seed_accounts
):
    js = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    assert "const numberRequired = false;" in js
    assert "Account Number <span" in js and "(optional)" in js
    assert "Remove unused seeded accounts" in js
    # and the books take an account with no number, as the schema always allowed
    for number in (None, "", "  "):
        r = client.post(
            "/api/accounts",
            json={
                "name": f"No number {number!r}",
                "account_type": "expense",
                "account_number": number,
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["account_number"] is None
    r = client.post(
        "/api/accounts",
        json={"name": "Numbered", "account_type": "expense", "account_number": "7777"},
    )
    assert r.status_code == 201 and r.json()["account_number"] == "7777"
    bad = client.post(
        "/api/accounts",
        json={"name": "Bad", "account_type": "expense", "account_number": "7x7"},
    )
    assert bad.status_code == 400


def test_unused_seeded_accounts_go_in_one_step_and_the_used_ones_stay(
    client, db_session, seed_accounts
):
    from app.services import control_accounts

    from app.routes.accounts import _FOUND_BY_NUMBER

    control = {n for n in seed_accounts if control_accounts.is_control_number(n)}
    free = sorted(
        n
        for n, a in seed_accounts.items()
        if n not in control
        and n not in _FOUND_BY_NUMBER  # payroll's stay for their own reason
        and a.account_type == AccountType.EXPENSE
    )
    posted_no, parent_no = free[0], free[1]
    # one seeded account with a posting, one with a sub-account: both stay
    lines = [
        {
            "account_id": seed_accounts[posted_no].id,
            "debit": Decimal("40.00"),
            "credit": Decimal("0"),
            "description": "Flyers",
        },
        {
            "account_id": seed_accounts["1000"].id,
            "debit": Decimal("0"),
            "credit": Decimal("40.00"),
            "description": "Flyers",
        },
    ]
    create_journal_entry(db_session, date(2026, 3, 1), "Print shop", lines)
    db_session.add(
        Account(
            name="Trade shows",
            account_type=AccountType.EXPENSE,
            account_number=None,
            balance=Decimal("0"),
            parent_id=seed_accounts[parent_no].id,
        )
    )
    db_session.commit()
    before = db_session.query(Account).filter(Account.is_system.is_(True)).count()
    preview = client.get("/api/accounts/unused-seeded").json()
    removable = {a["account_number"] for a in preview["removable"]}
    kept = {a["account_number"]: a["reason"] for a in preview["kept"]}
    assert kept[posted_no] == "has postings"
    assert kept[parent_no] == "has sub-accounts"
    assert all(kept[n] == "a control account" for n in control), kept
    assert removable and not (removable & control)
    assert posted_no not in removable and parent_no not in removable
    assert (
        db_session.query(Account).filter(Account.is_system.is_(True)).count() == before
    )
    done = client.post("/api/accounts/unused-seeded/remove", json={}).json()
    assert {a["account_number"] for a in done["removed"]} == removable
    after = db_session.query(Account).filter(Account.is_system.is_(True)).all()
    assert len(after) == before - len(removable)
    assert all(a.account_number not in removable for a in after)
    assert db_session.query(TransactionLine).count() == 2  # nothing posted was touched
    again = client.get("/api/accounts/unused-seeded").json()
    assert again["removable"] == []  # nothing left to remove


# ---------------------------------------------------------------- #335


def test_the_vendor_list_has_a_search_box_and_a_vendor_its_paid_to_date_and_csv(
    client, db_session, seed_accounts
):
    js = (ROOT / "app/static/js/vendors.js").read_text(encoding="utf-8")
    assert 'id="vendors-filter"' in js and "VendorsPage.filterRows" in js
    assert "data-vendor-row" in js
    assert "Paid this year" in js and "/payments/csv" in js
    vendor = Vendor(name="City Water")
    db_session.add(vendor)
    db_session.flush()
    for day, amount, voided in (
        (date(2026, 1, 15), "120.00", False),
        (date(2026, 2, 15), "130.00", False),
        (date(2025, 12, 15), "100.00", False),
        (date(2026, 3, 15), "999.00", True),
    ):
        db_session.add(
            BillPayment(
                vendor_id=vendor.id,
                date=day,
                amount=Decimal(amount),
                method="check",
                check_number="10" + amount[:2],
                is_voided=voided,
            )
        )
    db_session.commit()
    r = client.get(f"/api/vendors/{vendor.id}/payments/csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "Payments_City_Water.csv" in r.headers["content-disposition"]
    text = r.text
    assert "2026-01-15" in text and "2025-12-15" in text and "yes" in text
    assert "Total paid,350.00" in text  # the voided one aside
    year = client.get(
        f"/api/vendors/{vendor.id}/payments/csv",
        params={"start_date": "2026-01-01", "end_date": "2026-12-31"},
    ).text
    assert "2025-12-15" not in year and "Total paid,250.00" in year
    assert client.get("/api/vendors/99999/payments/csv").status_code == 404
