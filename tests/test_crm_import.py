"""The in-house CRM's CSV export, imported (app/services/crm_import.py).

A small invented export shaped like the real one: contacts keyed by email,
one with two rows for one email and one with none ("Name (Company)"); a
two-line taxed invoice; a duplicate payment and its negative reversal; an
open invoice; an expense and an owner's draw. The plan writes match files
and changes nothing; edits to them are read back; the import posts what the
screens post; a second import adds nothing.
"""

import csv
from datetime import date
from decimal import Decimal

import pytest

from app.models.accounts import Account
from app.models.contacts import Customer, Vendor
from app.models.invoices import Invoice, InvoiceStatus
from app.models.items import Item
from app.models.payments import Payment
from app.models.transactions import Transaction
from app.services import crm_import

FILES = {
    "contacts.csv": [
        [
            "firstName",
            "lastName",
            "email",
            "phone",
            "businessPhone",
            "company",
            "address",
            "source",
            "tags",
            "createdAt",
        ],
        ["Ada", "Lane", "ada@example.test", "555-0100", "", "", "", "", "", ""],
        [
            "Ada",
            "Lane",
            "ada@example.test",
            "555-0100",
            "555-0199",
            "Lane Dental",
            "12 Oak St\nGretna, LA 70053",
            "",
            "",
            "",
        ],
        ["Bo", "Reed", "", "", "", "Reed Tires", "", "", "", ""],
    ],
    "invoices.csv": [
        [
            "invoiceNumber",
            "contactEmail",
            "status",
            "dueDate",
            "sentAt",
            "currency",
            "lineTitle",
            "lineDescription",
            "quantity",
            "unitPrice",
            "lineAmount",
            "taxable",
            "subtotal",
            "discountAmount",
            "taxAmount",
            "total",
            "paidAmount",
            "creditedAmount",
        ],
        [
            "101",
            "ada@example.test",
            "paid",
            "2026-01-31T00:00:00+00:00",
            "2026-01-16T12:00:00+00:00",
            "USD",
            "consulting",
            "Setup",
            "2",
            "$95.00 ",
            "$190.00 ",
            "FALSE",
            "$590.00 ",
            "$0.00 ",
            "$41.00 ",
            "$631.00 ",
            "$631.00 ",
            "$0.00 ",
        ],
        [
            "101",
            "ada@example.test",
            "paid",
            "2026-01-31T00:00:00+00:00",
            "2026-01-16T12:00:00+00:00",
            "USD",
            "Router",
            "",
            "1",
            "$400.00 ",
            "$400.00 ",
            "TRUE",
            "$590.00 ",
            "$0.00 ",
            "$41.00 ",
            "$631.00 ",
            "$631.00 ",
            "$0.00 ",
        ],
        [
            "102",
            "Bo Reed (Reed Tires)",
            "sent",
            "2026-03-15T00:00:00+00:00",
            "2026-03-01T09:00:00+00:00",
            "USD",
            "consulting",
            "Visit",
            "1",
            "$95.00 ",
            "$95.00 ",
            "FALSE",
            "$95.00 ",
            "$0.00 ",
            "$0.00 ",
            "$95.00 ",
            "$0.00 ",
            "$0.00 ",
        ],
    ],
    "payments.csv": [
        ["invoiceNumber", "amount", "method", "note", "paidAt"],
        ["101", "$631.00", "other", "Backfilled payment", "2026-01-20T00:00:00+00:00"],
        ["101", "'-$631.00", "other", "Reversal", "2026-02-01T00:00:00+00:00"],
        ["101", "$631.00", "check", "4417", "2026-01-21T00:00:00+00:00"],
    ],
    "products_and_services.csv": [
        ["name", "description", "unitPrice", "incomeCategoryName"],
        ["consulting", "", "$95.00", ""],
        ["Toner", "", "$80.00", "Toner Sales"],
    ],
    "expenses.csv": [
        [
            "payee",
            "billDate",
            "paidDate",
            "status",
            "categoryName",
            "lineTitle",
            "lineDescription",
            "lineAmount",
            "subtotal",
            "taxAmount",
            "total",
        ],
        [
            "Hostly (Hostly)",
            "2026-01-05",
            "2026-01-06",
            "paid",
            "Web Hosting",
            "",
            "Monthly hosting",
            "$12.50",
            "$12.50",
            "$0.00",
            "$12.50",
        ],
    ],
    "account_transactions.csv": [
        ["occurredOn", "fromAccount", "toAccount", "amount", "memo", "reversalOfDate"],
        ["2026-02-03", "Business Checking", "Owner's Draws", "$100.00", "", ""],
        ["2026-02-04", "Business Checking", "Owner's Draws", "$40.00", "ATM", ""],
        # the $40 coming back: a reversal posts as the movement back
        [
            "2026-02-05",
            "Owner's Draws",
            "Business Checking",
            "$40.00",
            "",
            "2026-02-04",
        ],
    ],
    "credit_memos.csv": [["invoiceNumber", "amount", "reason", "issuedAt"]],
}


@pytest.fixture
def export(tmp_path):
    for name, rows in FILES.items():
        with open(tmp_path / name, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
    return tmp_path


def _match(export, name):
    with open(export / "match" / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _rewrite(export, name, rows):
    with open(export / "match" / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_money_reads_the_exports_formats():
    assert crm_import.money("$1,234.56 ") == Decimal("1234.56")
    assert crm_import.money("'-$60.00") == Decimal("-60.00")
    assert crm_import.money("(5.00)") == Decimal("-5.00")
    assert crm_import.money("") == Decimal("0")


def test_the_plan_matches_and_writes_nothing(export, db_session, seed_accounts):
    accounts = db_session.query(Account).count()
    plan = crm_import.build_plan(db_session, export)
    crm_import.write_match_files(plan, export)

    assert plan.problems == []
    customers = {r["source"]: r for r in _match(export, "customers.csv")}
    # two rows for one email: the one naming the company, with its details
    assert customers["ada@example.test"]["slowbooks_name"] == "Lane Dental"
    assert customers["ada@example.test"]["phone"] == "555-0199"
    assert customers["Bo Reed (Reed Tires)"]["slowbooks_name"] == "Reed Tires"
    accts = {
        (r["used_as"], r["source_name"]): r for r in _match(export, "accounts.csv")
    }
    assert accts[("transfer", "Business Checking")]["slowbooks_account"] == "Checking"
    assert accts[("transfer", "Owner's Draws")]["slowbooks_account"] == "Owner's Draw"
    assert accts[("expense", "Web Hosting")]["new_account_type"] == "expense"
    assert accts[("income", "Toner Sales")]["new_account_type"] == "income"
    # the duplicate and its reversal cancel; the check stays
    assert [(p["amount"], p["method"]) for p in plan.payments] == [
        (Decimal("631.00"), "Check")
    ]
    assert plan.totals["open_ar"] == Decimal("95.00")
    assert (
        "Open A/R: 1 invoices, $95.00" in (export / "match" / "report.txt").read_text()
    )
    assert db_session.query(Account).count() == accounts
    assert db_session.query(Customer).count() == 0


def test_edits_to_the_match_files_win(export, db_session, seed_accounts):
    crm_import.write_match_files(crm_import.build_plan(db_session, export), export)
    rows = _match(export, "customers.csv")
    for r in rows:
        r["slowbooks_name"] = "One Customer"  # merge both
    _rewrite(export, "customers.csv", rows)
    rows = _match(export, "accounts.csv")
    for r in rows:
        if r["source_name"] == "Web Hosting":
            r["slowbooks_account"] = "Telephone & Internet"
    _rewrite(export, "accounts.csv", rows)

    plan = crm_import.build_plan(db_session, export)
    assert plan.totals["customers"] == 1
    crm_import.apply_plan(db_session, plan)
    assert [c.name for c in db_session.query(Customer)] == ["One Customer"]
    assert db_session.query(Account).filter(Account.name == "Web Hosting").count() == 0


def test_an_unresolved_match_blocks_the_import(export, db_session, seed_accounts):
    crm_import.write_match_files(crm_import.build_plan(db_session, export), export)
    rows = _match(export, "accounts.csv")
    for r in rows:
        if r["source_name"] == "Web Hosting":
            r["slowbooks_account"], r["new_account_type"] = "Hosting Costs", ""
    _rewrite(export, "accounts.csv", rows)
    plan = crm_import.build_plan(db_session, export)
    assert any("Hosting Costs" in p for p in plan.problems)
    with pytest.raises(ValueError):
        crm_import.apply_plan(db_session, plan)


def test_the_import_posts_what_the_screens_post(export, db_session, seed_accounts):
    made = crm_import.apply_plan(db_session, crm_import.build_plan(db_session, export))
    assert made["invoices"] == 2 and made["payments"] == 1 and made["expenses"] == 1

    inv = db_session.query(Invoice).filter(Invoice.invoice_number == "101").one()
    assert (inv.status, inv.balance_due, inv.tax_amount, inv.total) == (
        InvoiceStatus.PAID,
        Decimal("0"),
        Decimal("41.00"),
        Decimal("631.00"),
    )
    assert inv.date.isoformat() == "2026-01-16"  # sent, the CRM's only invoice date
    assert inv.tax_rate == Decimal("0.102500")
    assert [
        ln.is_taxable for ln in sorted(inv.lines, key=lambda ln: ln.line_order)
    ] == [False, True]
    open_inv = db_session.query(Invoice).filter(Invoice.invoice_number == "102").one()
    assert (open_inv.status, open_inv.balance_due) == (
        InvoiceStatus.SENT,
        Decimal("95.00"),
    )

    by_name = {a.name: a for a in db_session.query(Account)}
    assert by_name["Accounts Receivable"].balance == Decimal("95.00")
    assert by_name["Sales Tax Payable"].balance == Decimal("41.00")
    # 631 in, 12.50 hosting and the 100 draw out; the $40 draw came back
    assert by_name["Checking"].balance == Decimal("518.50")
    # equity is kept credit-positive: a draw takes it down
    assert by_name["Owner's Draw"].balance == Decimal("-100.00")
    assert by_name["Web Hosting"].balance == Decimal("12.50")
    assert (
        db_session.query(Item).filter(Item.name == "Toner").one().income_account_id
        == by_name["Toner Sales"].id
    )
    assert [v.name for v in db_session.query(Vendor)] == ["Hostly"]
    pay = db_session.query(Payment).one()
    assert (pay.method, pay.check_number) == ("Check", "4417")
    # every posting balances
    for txn in db_session.query(Transaction):
        assert sum(ln.debit for ln in txn.lines) == sum(ln.credit for ln in txn.lines)


def test_a_second_import_adds_nothing(export, db_session, seed_accounts):
    crm_import.apply_plan(db_session, crm_import.build_plan(db_session, export))
    txns = db_session.query(Transaction).count()
    made = crm_import.apply_plan(db_session, crm_import.build_plan(db_session, export))
    assert made == {}
    assert db_session.query(Transaction).count() == txns


def test_the_bank_is_brought_to_its_real_balance(export, db_session, seed_accounts):
    plan = crm_import.build_plan(db_session, export)
    crm_import.apply_plan(db_session, plan)
    checking = db_session.query(Account).filter(Account.name == "Checking").one()
    assert checking.balance == Decimal("518.50")
    on = date(2026, 3, 31)
    assert crm_import.true_up_bank(db_session, plan, Decimal("200.00"), on) is not None
    db_session.refresh(checking)
    assert checking.balance == Decimal("200.00")
    equity = db_session.query(Account).filter(Account.account_number == "3900").one()
    assert equity.name == "Opening Balance Equity"
    assert equity.balance == Decimal("-318.50")
    # run again: the books already agree, nothing more posts
    assert crm_import.true_up_bank(db_session, plan, Decimal("200.00"), on) is None
