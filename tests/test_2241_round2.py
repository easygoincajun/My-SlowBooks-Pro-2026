"""2.24.1 round 2: W-27 (both platforms; the owner's blocker) and NEW-168.

- W-27: the QuickBooks Desktop ledger export with an Account column inserted
  as column A, ahead of the section headings, was not recognized by the
  guard (its heading search started at the leftmost mapped column, which
  was Account itself) and the file read as sales, as on 2.24.0. The guard
  now looks for the headings left of the report's own columns and never
  reads the Account column as one, wherever it was inserted.
- NEW-168: a check with no number saved as ``Check.pdf`` into Reports, not
  Documents: the desktop read a PDF's kind off the name before the first
  underscore, and ``Check.pdf`` had none. It reads the stem now.
"""

import csv
import io
from pathlib import Path

import pytest

from app.services.qb_ledger_import import LEDGER_COLUMNS, has_account_sections
from app.services.qb_report_import import detect_report_type

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/qb_desktop/Transaction_Detail_by_Account.csv"
REFUSAL = "added Account column"


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


def _header(rows):
    at = next(
        i
        for i, r in enumerate(rows)
        if all(c in [x.strip() for x in r] for c in LEDGER_COLUMNS)
    )
    cells = [x.strip() for x in rows[at]]
    return at, {name: cells.index(name) for name in cells if name}


def with_account_inserted(text, at):
    """The export with an Account column inserted at index ``at`` (0 = column
    A, ahead of the headings; a column name = before that column; None =
    appended), each transaction row carrying its section's account, the way
    a person fills it in a spreadsheet."""
    rows = _rows(text)
    header_at, cols = _header(rows)
    first = min(cols.values())
    idx = (
        len(rows[header_at])
        if at is None
        else (at if isinstance(at, int) else cols[at])
    )
    out, heading = [], None
    for i, r in enumerate(rows):
        r = list(r)
        if i < header_at:
            out.append(r)
            continue
        if i == header_at:
            out.append(r[:idx] + ["Account"] + r[idx:])
            continue
        ttype = r[cols["Type"]].strip() if cols["Type"] < len(r) else ""
        if not ttype:
            label = next((c.strip() for c in r[:first] if c.strip()), "")
            if label:
                heading = (
                    None if label.startswith("Total ") or label == "TOTAL" else label
                )
            out.append(r[:idx] + [""] + r[idx:])
        else:
            out.append(r[:idx] + [heading or ""] + r[idx:])
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(out)
    return buf.getvalue()


@pytest.fixture
def export():
    return FIXTURE.read_bytes().decode("cp1252")


@pytest.mark.parametrize(
    "where", [0, "Split", None], ids=["column A", "before Split", "last"]
)
def test_the_guard_sees_the_sections_wherever_the_account_column_was_put(export, where):
    text = with_account_inserted(export, where)
    rows = _rows(text)
    header_at, cols = _header(rows)
    assert "Account" in cols
    assert has_account_sections(rows, header_at, cols)
    assert detect_report_type(text) == "ledger"


def test_the_default_export_is_still_the_ledger_and_has_no_account_column(export):
    rows = _rows(export)
    header_at, cols = _header(rows)
    assert "Account" not in cols
    assert detect_report_type(export) == "ledger"


@pytest.mark.parametrize("where", [0, "Split"], ids=["column A", "before Split"])
def test_the_inserted_column_is_refused_and_posts_nothing(client, export, where):
    from app.models.invoices import Invoice
    from app.models.transactions import Transaction

    text = with_account_inserted(export, where)
    r = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("ledger.csv", text.encode("cp1252"), "text/csv")},
    )
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body.get("detected") == "ledger"
    assert not body.get("journals") and not body.get("imported")
    assert any(REFUSAL in e for e in body.get("errors", [])), body.get("errors")
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        assert db.query(Invoice).count() == 0
        assert db.query(Transaction).count() == 0
    finally:
        db.close()


def test_a_sales_export_with_its_own_account_column_is_not_a_ledger():
    # the sales reports carry an Account column of their own and no section
    # headings: the guard must leave them alone (the fixtures read as before)
    for name, kind in (
        ("qb_transaction_detail_sales_receipts.csv", "sales_receipts"),
        ("qb_transaction_detail_invoices.csv", "invoices"),
        ("qb_check_detail.csv", "checks"),
        ("qb_deposit_detail.csv", "deposits"),
    ):
        text = (ROOT / "tests/fixtures" / name).read_text(encoding="utf-8-sig")
        assert detect_report_type(text) == kind, name


def test_a_check_with_no_number_is_a_document_on_the_desktop():
    import desktop_launcher

    assert desktop_launcher._folder_for("Check.pdf") == "Documents"
    assert desktop_launcher._folder_for("Check_1072.pdf") == "Documents"
    assert desktop_launcher._folder_for("Invoice_1001.pdf") == "Documents"
    assert desktop_launcher._folder_for("Profit_and_Loss.pdf") == "Reports"
    assert desktop_launcher._folder_for("Trial Balance.pdf") == "Reports"
