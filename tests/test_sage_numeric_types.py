"""Numeric account types from a real Sage 50 US 2026 chart export."""

from pathlib import Path

import pytest

from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction, TransactionLine
from app.services.sage_import import parse_coa

CHART = (Path(__file__).parent / "fixtures/sage50_2026/chart.csv").read_text(
    encoding="utf-8"
)


def test_sage_2026_export_maps_account_types():
    accounts, errors = parse_coa(CHART)
    assert errors == []
    assert [(a["name"], a["type"]) for a in accounts] == [
        ("Petty Cash", AccountType.ASSET),
        ("Accounts Receivable", AccountType.ASSET),
        ("Inventory", AccountType.ASSET),
        ("Prepaid Expenses", AccountType.ASSET),
        ("Furniture and Fixtures", AccountType.ASSET),
        ("Accum. Depreciation-Furniture", AccountType.ASSET),
        ("Deposits", AccountType.ASSET),
        ("Accounts Payable", AccountType.LIABILITY),
        ("Accrued Expenses", AccountType.LIABILITY),
        ("Notes Payable-Noncurrent", AccountType.LIABILITY),
        ("Beginning Balance Equity", AccountType.EQUITY),
        ("Retained Earnings", AccountType.EQUITY),
        ("Dividends Paid", AccountType.EQUITY),
        ("Sales", AccountType.INCOME),
        ("Product Cost", AccountType.COGS),
        ("Freight", AccountType.EXPENSE),
    ]
    assert accounts[0]["code"] == "10000-00"


# Documented types absent from Bellwether: synthetic parser cases.
@pytest.mark.parametrize(
    "code,expected", [("3", AccountType.ASSET), ("11", AccountType.LIABILITY)]
)
def test_sage_retainage_numeric_types(code, expected):
    accounts, errors = parse_coa(
        f"Account ID,Account Description,Account Type\nR1,Retainage,{code}\n"
    )
    assert errors == []
    assert accounts[0]["type"] == expected


@pytest.mark.parametrize(
    "code", ["7", "9", "13", "15", "17", "20", "22", "99", "-1", "0.5"]
)
def test_sage_unknown_numeric_type_is_rejected(code):
    accounts, errors = parse_coa(
        f"Account ID,Account Description,Account Type\nX1,Unknown,{code}\n"
    )
    assert accounts == []
    assert errors == [f"COA row 2: unmapped Sage account type {code!r}"]


def test_sage_numeric_chart_import_resolves_ledger_account_ids(client, db_session):
    # Synthetic balanced ledger exercises the existing import path with real COA.
    ledger = (
        "Date,Reference,Jrnl,Account ID,Trans Description,Debit Amt,Credit Amt\n"
        "03/01/2026,TEST001,CRJ,10000-00,Test receipt,25,\n"
        "03/01/2026,TEST001,CRJ,40000-00,Test receipt,,25\n"
    )
    files = [
        ("files", ("chart.csv", CHART.encode(), "text/csv")),
        ("files", ("generalledger.csv", ledger.encode(), "text/csv")),
    ]
    for action in ("dry-run", "import"):
        response = client.post(f"/api/migration/sage/{action}", files=files)
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True, response.json()
    result = response.json()
    assert result["imported_accounts"] == 16
    assert result["imported_journals"] == 1
    account = db_session.query(Account).filter_by(name="Petty Cash").one()
    assert account.account_type == AccountType.ASSET
    txn = db_session.query(Transaction).filter_by(source_type="sage_import").one()
    lines = db_session.query(TransactionLine).filter_by(transaction_id=txn.id).all()
    assert len(lines) == 2
    assert sum(line.debit or 0 for line in lines) == 25
    assert sum(line.credit or 0 for line in lines) == 25
