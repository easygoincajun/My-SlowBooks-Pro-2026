"""Synthetic reproduction of discussion #349; no customer records."""

import csv
import io
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import Base
from app.models.invoices import Invoice


def report(checking, income, extra_account, title=True):
    columns = [
        "",
        "Type",
        "Date",
        "Num",
        "Name",
        "Memo",
        "Split",
        "Debit",
        "Credit",
        "Balance",
    ]
    if extra_account:
        columns.append("Account")
    rows = [
        {},
        {"": checking},
        {
            "Type": "Sales Receipt",
            "Date": "01/05/2024",
            "Num": "SYN-349",
            "Name": "Synthetic Customer",
            "Memo": "Synthetic sale",
            "Split": "-SPLIT-",
            "Debit": "100.00",
            "Balance": "100.00",
            "Account": checking,
        },
        {"": "Total " + checking, "Debit": "100.00", "Balance": "100.00"},
        {"": income},
        {
            "Type": "Sales Receipt",
            "Date": "01/05/2024",
            "Num": "SYN-349",
            "Name": "Synthetic Customer",
            "Memo": "Synthetic sale",
            "Split": checking,
            "Credit": "100.00",
            "Balance": "-100.00",
            "Account": income,
        },
        {"": "Total " + income, "Credit": "100.00", "Balance": "-100.00"},
    ]
    out = io.StringIO()
    if title:
        out.write("Synthetic company\nTransaction Detail by Account\n")
    writer = csv.DictWriter(out, columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows[1:])
    return out.getvalue()


@pytest.mark.parametrize("extra_account", [False, True])
@pytest.mark.parametrize("title", [False, True])
def test_ledger_report_never_creates_sales_documents(
    client, db_session, seed_accounts, extra_account, title
):
    data = report(
        seed_accounts["1000"].name, seed_accounts["4000"].name, extra_account, title
    )
    before = snapshot(db_session)
    response = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("renamed-export.csv", data.encode(), "text/csv")},
    )
    result = response.json()
    documents = db_session.query(Invoice).count()
    assert response.status_code == 200
    assert documents == 0, "A ledger-history report must not create sales documents"
    if extra_account:
        assert snapshot(db_session) == before
        assert result["journals"] == result["sales_receipts"] == result["invoices"] == 0
        assert "Nothing was imported" in result["errors"][0]
        assert "Filters > Account > All accounts" in result["errors"][0]
    else:
        assert result["journals"] == 1 and result["errors"] == []


def snapshot(db):
    """Compare every persisted field, including existing records, after refusal."""
    return {
        table.name: sorted((tuple(row) for row in db.execute(select(table))), key=repr)
        for table in Base.metadata.sorted_tables
    }


def test_interleaved_ledger_fixture_with_account_column_is_refused(
    client, db_session, seed_accounts
):
    fixture = (
        Path(__file__).parent / "fixtures/qb_desktop/Transaction_Detail_by_Account.csv"
    )
    rows = list(csv.reader(io.StringIO(fixture.read_bytes().decode("cp1252"))))
    type_at = rows[0].index("Type")
    rows[0].append("Account")
    heading = ""
    for row in rows[1:]:
        if not row[type_at]:
            label = next((c.strip() for c in row[:type_at] if c.strip()), "")
            if label:
                heading = label
        row.append(heading if row[type_at] else "")
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    before = snapshot(db_session)
    result = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("export.csv", out.getvalue().encode(), "text/csv")},
    ).json()
    assert "Nothing was imported" in result["errors"][0]
    assert snapshot(db_session) == before


def test_late_ledger_section_refuses_the_whole_file(client, db_session, seed_accounts):
    # A valid sales report comes first. No document may be posted before the
    # dispatcher notices the later ledger section.
    fixture = (
        Path(__file__).parent / "fixtures/qb_transaction_detail_sales_receipts.csv"
    )
    first = list(csv.reader(io.StringIO(fixture.read_text(encoding="utf-8"))))
    header_at = next(
        i for i, row in enumerate(first) if "Account" in row and "Date" in row
    )
    columns = first[header_at] + ["Type"]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in first[header_at + 1 :]:
        writer.writerow(
            row + ["Sales Receipt" if len(row) > 1 and "/" in row[1] else ""]
        )
    ledger = list(
        csv.DictReader(
            io.StringIO(
                report(
                    seed_accounts["1000"].name, seed_accounts["4000"].name, True, False
                )
            )
        )
    )
    mapped = csv.DictWriter(out, columns, extrasaction="ignore")
    mapped.writerows(ledger)
    before = snapshot(db_session)
    result = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("mixed.csv", out.getvalue().encode(), "text/csv")},
    ).json()
    assert result["errors"] and "Nothing was imported" in result["errors"][0]
    assert snapshot(db_session) == before
