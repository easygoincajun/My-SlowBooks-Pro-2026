"""The invoices still open at a QuickBooks Online migration come across as
invoices (2.23.2, the release gate's NEW-120).

The Export data zip carries the history: every invoice and payment posts as
a journal entry on the export's own receivable account, "Accounts Receivable
(A/R)". The A/R aging reads documents, so after the import it was empty and
the open balance sat on that account with nothing behind it. QuickBooks
Online's A/R Aging Detail report (Reports → A/R Aging Detail → Export to
Excel), uploaded beside the zip, lists what was open: each invoice with its
number, dates and open balance, and each payment not (fully) applied. They
are carried over as invoices and unapplied payments whose postings move the
open balance from the export's receivable account to 1100, so the aging and
every customer's balance are right and that account ends at zero; no income
is touched, the sales being in the history already.

The fixture is the Sutehk sample's report in the layout of the real
download the importer was built against (that company's report, every row
matched to its Journal block, stays private).
"""

import io
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.accounts import Account, AccountType
from app.models.contacts import Customer
from app.models.invoices import Invoice, InvoiceStatus
from app.models.items import Item
from app.models.payments import Payment
from app.models.transactions import Transaction, TransactionLine
from app.services import qbo_export_import as qbo

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "qbo_export"
AGING = "Sutehk_Realty_Group_A_R_Aging_Detail_Report.csv"
BUNDLE = [
    "Balance_sheet.csv",
    "Profit_and_loss.csv",
    "Journal.csv",
    "General_ledger.csv",
    "Trial_balance.csv",
    "Customers.csv",
    "Vendors.csv",
    "Employees.csv",
]


def _files(aging_text=None, replace=None):
    """The bundle as CSVs, with the aging report (as given, or the
    fixture's); `replace` rewrites names in every file (a renamed
    account)."""
    out = []
    for name in BUNDLE:
        text = (FIXTURES / name).read_text()
        for old, new in (replace or {}).items():
            text = text.replace(old, new)
        out.append(("files", (name, io.BytesIO(text.encode()), "text/csv")))
    if aging_text is not False:
        text = aging_text if aging_text is not None else (FIXTURES / AGING).read_text()
        for old, new in (replace or {}).items():
            text = text.replace(old, new)
        out.append(("files", (AGING, io.BytesIO(text.encode()), "text/csv")))
    return out


def _balance(client, name=None, number=None):
    for account in client.get("/api/accounts").json():
        if (name and account["name"] == name) or (
            number and account.get("account_number") == number
        ):
            return Decimal(str(account["balance"]))
    raise AssertionError(f"no account {name or number}")


def _imported(client):
    data = client.post("/api/migration/qbo_export/import", files=_files()).json()
    assert data["ok"] is True, data["errors"]
    return data


# ── the report ─────────────────────────────────────────────────────────────


def test_the_report_is_told_apart_by_name():
    assert qbo.classify_filename(AGING.replace(".csv", ".xlsx")) == "ar_aging"
    assert qbo.classify_filename("Any Co_A_R_Aging_Detail_Report.xlsx") == "ar_aging"


def test_the_reports_rows_read_as_invoices_and_payments():
    items, errors, notes = qbo.parse_ar_aging((FIXTURES / AGING).read_text())
    assert errors == [] and notes == []
    assert [(i["kind"], i["num"], i["customer"], i["open"]) for i in items] == [
        ("payment", "4417", "Sam Ortiz - CUST", Decimal("-150")),
        ("invoice", "1004", "Sam Ortiz - CUST", Decimal("275")),
        ("invoice", "1003", "Dana Reyes - CUST", Decimal("500")),
        ("payment", "", "Dana Reyes - CUST", Decimal("-100")),
    ]
    partly_paid = items[2]
    assert partly_paid["amount"] == Decimal("800")
    assert partly_paid["date"].isoformat() == "2026-08-20"
    assert partly_paid["due_date"].isoformat() == "2026-09-19"
    assert items[0]["amount"] == Decimal("-600")  # 450 of it applied


# ── the dry run ────────────────────────────────────────────────────────────


def test_the_dry_run_counts_them_and_checks_them_against_the_journal(
    client, db_session, seed_accounts
):
    data = client.post("/api/migration/qbo_export/dry-run", files=_files()).json()
    assert data["ok"] is True, data["errors"]
    assert data["open_invoices"] == 2 and data["unapplied_payments"] == 2
    assert any(
        "come to 525.00, what the Journal leaves on Accounts Receivable (A/R): "
        "they move to 1100 Accounts Receivable" in w
        and "ends at zero" in w
        for w in data["warnings"]
    ), data["warnings"]
    assert db_session.query(Invoice).count() == 0
    assert db_session.query(Payment).count() == 0


# ── the import ─────────────────────────────────────────────────────────────


def test_the_open_invoices_and_unapplied_payments_come_across(
    client, db_session, seed_accounts
):
    data = _imported(client)
    assert data["imported_open_invoices"] == 2
    assert data["imported_unapplied_payments"] == 2
    invoices = {i.invoice_number: i for i in db_session.query(Invoice).all()}
    assert set(invoices) == {"1003", "1004"}
    partly = invoices["1003"]
    assert partly.customer.name == "Dana Reyes - CUST"
    assert partly.date.isoformat() == "2026-08-20"
    assert partly.due_date.isoformat() == "2026-09-19"
    assert partly.status == InvoiceStatus.SENT
    assert partly.total == Decimal("500.00") and partly.balance_due == Decimal("500.00")
    assert partly.tax_amount == 0
    (line,) = partly.lines
    assert line.item.name == qbo.CARRY_ITEM
    assert "invoice 1003" in line.description
    assert "for 800.00, 500.00 of it open" in line.description
    assert invoices["1004"].balance_due == Decimal("275.00")
    assert invoices["1004"].due_date.isoformat() == "2026-09-02"
    payments = {
        (p.customer.name, p.date.isoformat()): p for p in db_session.query(Payment)
    }
    sam = payments[("Sam Ortiz - CUST", "2026-06-30")]
    assert sam.amount == Decimal("150.00") and sam.reference == "4417"
    assert "a payment of 600.00, 150.00 of it not applied" in sam.notes
    dana = payments[("Dana Reyes - CUST", "2026-10-01")]
    assert dana.amount == Decimal("100.00") and dana.allocations == []
    # the open balance moved: the export's receivable account ends at zero,
    # 1100 holds what was open, with the documents behind it
    assert _balance(client, name="Accounts Receivable (A/R)") == 0
    assert _balance(client, number="1100") == Decimal("525.00")
    aging = client.get("/api/reports/ar-aging", params={"as_of_date": "2026-10-15"})
    totals = aging.json()["totals"]
    assert Decimal(str(totals["total"])) == Decimal("525.00")
    assert Decimal(str(totals["unapplied_credits"])) == Decimal("250.00")
    # no income touched: the sales are in the history already
    carried = (
        db_session.query(TransactionLine)
        .join(Transaction, Transaction.id == TransactionLine.transaction_id)
        .join(Account, Account.id == TransactionLine.account_id)
        .filter(Transaction.source_type.in_(("invoice", "payment")))
        .filter(Account.account_type == AccountType.INCOME)
        .count()
    )
    assert carried == 0
    item = db_session.query(Item).filter(Item.name == qbo.CARRY_ITEM).one()
    assert item.income_account_id == (
        db_session.query(Account.id)
        .filter(Account.name == "Accounts Receivable (A/R)")
        .scalar()
    )


def test_a_second_upload_carries_nothing_twice(client, db_session, seed_accounts):
    _imported(client)
    again = client.post("/api/migration/qbo_export/import", files=_files()).json()
    assert again["ok"], again["errors"]
    assert again["imported_open_invoices"] == 0
    assert again["imported_unapplied_payments"] == 0
    assert any(
        "All 4 rows of the A/R Aging Detail report are in the books already" in w
        for w in again["warnings"]
    ), again["warnings"]
    assert db_session.query(Invoice).count() == 2
    assert db_session.query(Payment).count() == 2
    assert _balance(client, number="1100") == Decimal("525.00")
    dry = client.post("/api/migration/qbo_export/dry-run", files=_files()).json()
    assert dry["open_invoices"] == 0 and dry["unapplied_payments"] == 0


def test_a_carried_payment_applies_to_a_carried_invoice(
    client, db_session, seed_accounts
):
    _imported(client)
    invoice = db_session.query(Invoice).filter(Invoice.invoice_number == "1003").one()
    credit = (
        db_session.query(Payment)
        .join(Customer, Customer.id == Payment.customer_id)
        .filter(Customer.name == "Dana Reyes - CUST")
        .one()
    )
    resp = client.post(
        f"/api/payments/{credit.id}/apply",
        json={"allocations": [{"invoice_id": invoice.id, "amount": "100.00"}]},
    )
    assert resp.status_code == 200, resp.text
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("400.00")
    # the credit was in 1100 already: applying it posts nothing
    assert _balance(client, number="1100") == Decimal("525.00")


def test_voiding_a_carried_invoice_moves_its_balance_back(
    client, db_session, seed_accounts
):
    _imported(client)
    invoice = db_session.query(Invoice).filter(Invoice.invoice_number == "1004").one()
    resp = client.post(f"/api/invoices/{invoice.id}/void")
    assert resp.status_code == 200, resp.text
    assert _balance(client, number="1100") == Decimal("250.00")
    assert _balance(client, name="Accounts Receivable (A/R)") == Decimal("275.00")


def test_new_invoices_continue_past_the_carried_numbers(
    client, db_session, seed_accounts
):
    from app.services.numbering import next_invoice_number

    _imported(client)
    assert next_invoice_number(db_session) == "1005"


# ── what is left where it is, and why ──────────────────────────────────────


def test_a_report_that_disagrees_with_the_journal_says_by_how_much(
    client, db_session, seed_accounts
):
    text = (FIXTURES / AGING).read_text()
    short = "\n".join(
        ln for ln in text.splitlines() if ",10/01/2026,Payment," not in ln
    )
    data = client.post(
        "/api/migration/qbo_export/dry-run", files=_files(short + "\n")
    ).json()
    assert data["ok"] is True, data["errors"]
    assert data["unapplied_payments"] == 1
    assert any(
        "open balances come to 625.00, and the Journal leaves 525.00" in w
        and "the difference, -100.00, stays on Accounts Receivable (A/R)" in w
        for w in data["warnings"]
    ), data["warnings"]


def test_a_row_of_another_kind_stays_with_a_note(client, db_session, seed_accounts):
    text = (
        (FIXTURES / AGING)
        .read_text()
        .replace(
            ",10/01/2026,Payment,,Dana Reyes - CUST,10/01/2026,-100,-100\n",
            ",10/01/2026,Payment,,Dana Reyes - CUST,10/01/2026,-100,-100\n"
            ",10/05/2026,Credit Memo,CM-7,Dana Reyes - CUST,10/05/2026,-50,-50\n",
        )
    )
    items, errors, notes = qbo.parse_ar_aging(text)
    assert errors == [] and len(items) == 4
    assert any("Credit Memo CM-7" in n and "not carried over" in n for n in notes)
    data = client.post("/api/migration/qbo_export/import", files=_files(text)).json()
    assert data["ok"] is True, data["errors"]
    assert data["imported_open_invoices"] == 2
    assert any("Credit Memo CM-7" in w for w in data["warnings"])


def test_an_ap_aging_report_is_not_read_as_invoices(client, db_session, seed_accounts):
    text = (
        (FIXTURES / AGING)
        .read_text()
        .replace("A/R Aging Detail Report", "A/P Aging Detail Report")
    )
    data = client.post("/api/migration/qbo_export/import", files=_files(text)).json()
    assert data["ok"] is True, data["errors"]
    assert data["imported_open_invoices"] == 0
    assert any("is not read" in w for w in data["warnings"]), data["warnings"]
    assert db_session.query(Invoice).count() == 0


def test_a_name_off_the_customer_list_is_matched_or_created(
    client, db_session, seed_accounts
):
    text = (
        (FIXTURES / AGING)
        .read_text()
        # the list's Full Name for Dana, and a name the list does not have
        .replace(
            ",08/20/2026,Invoice,1003,Dana Reyes - CUST,",
            ",08/20/2026,Invoice,1003,Dana Reyes,",
        )
        .replace(
            ",09/02/2026,Invoice,1004,Sam Ortiz - CUST,",
            ",09/02/2026,Invoice,1004,Riley Park,",
        )
    )
    data = client.post("/api/migration/qbo_export/import", files=_files(text)).json()
    assert data["ok"] is True, data["errors"]
    assert any(
        "Riley Park is on the A/R Aging Detail report but not on the Customer list" in w
        for w in data["warnings"]
    ), data["warnings"]
    invoices = {i.invoice_number: i for i in db_session.query(Invoice).all()}
    assert invoices["1003"].customer.name == "Dana Reyes - CUST"
    assert invoices["1004"].customer.name == "Riley Park"


def test_a_renamed_receivable_account_is_found_by_its_section(
    client, db_session, seed_accounts
):
    rename = {"Accounts Receivable (A/R)": "Trade Receivables"}
    data = client.post(
        "/api/migration/qbo_export/import", files=_files(replace=rename)
    ).json()
    assert data["ok"] is True, data["errors"]
    assert data["imported_open_invoices"] == 2
    assert _balance(client, name="Trade Receivables") == 0
    assert _balance(client, number="1100") == Decimal("525.00")


def test_a_receivable_named_like_1100_is_refused_before_anything_is_written(
    client, db_session, seed_accounts
):
    control = db_session.query(Account).filter(Account.account_number == "1100").one()
    rename = {"Accounts Receivable (A/R)": control.name}
    data = client.post(
        "/api/migration/qbo_export/import", files=_files(replace=rename)
    ).json()
    assert data["ok"] is False
    assert any("would count twice" in e for e in data["errors"]), data["errors"]
    assert data["imported_journals"] == 0
    assert (
        db_session.query(Transaction)
        .filter(Transaction.source_type == qbo.SOURCE_TYPE)
        .count()
        == 0
    )


# ── as QuickBooks Online hands it over ─────────────────────────────────────


def _real_layout_workbook(csv_text: str) -> bytes:
    """The report as the real download types it: figures as numbers with
    the money format, totals as formulas, dates as text, the title rows and
    the run time merged across the report."""
    import csv

    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    rows = list(csv.reader(io.StringIO(csv_text)))
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row, start=1):
            if value == "":
                continue
            cell = sheet.cell(row=r, column=c)
            try:
                cell.value = (
                    float(value) if c >= 7 and not value.startswith("=") else value
                )
                if c >= 7 and not value.startswith("="):
                    cell.number_format = "#,##0.00"
            except ValueError:
                cell.value = value
    for r in (1, 2, 3, len(rows)):
        sheet.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_the_report_as_downloaded_reads_the_same(client, db_session, seed_accounts):
    from app.routes.migration import _sheet_as_csv

    workbook = _real_layout_workbook((FIXTURES / AGING).read_text())
    assert _sheet_as_csv(workbook) == (FIXTURES / AGING).read_text()
    files = _files(aging_text=False) + [
        (
            "files",
            (
                AGING.replace(".csv", ".xlsx"),
                io.BytesIO(workbook),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        )
    ]
    data = client.post("/api/migration/qbo_export/dry-run", files=files).json()
    assert data["ok"] is True, data["errors"]
    assert data["open_invoices"] == 2 and data["unapplied_payments"] == 2
