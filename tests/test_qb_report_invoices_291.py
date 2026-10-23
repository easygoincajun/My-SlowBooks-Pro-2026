"""Invoices from a QuickBooks Desktop Transaction Detail report CSV (#291).

QuickBooks Desktop's own IIF export is lists only, so a Desktop user's
invoices never arrived by IIF, and the report path covered sales receipts,
deposits and checks but not invoices. The same report filtered to Invoice
now imports each invoice with its lines (item, quantity, price, tax), its
Customer:Job and its number; open, partly paid or paid by the report's
Open Balance column, with a payment for what was paid; and the kind is
told from the header row's account (Accounts Receivable for an invoice,
the deposit account for a receipt), so one file can hold both.
"""

from decimal import Decimal
from pathlib import Path

from app.services.qb_report_import import (
    detect_report_type,
    import_invoice_report,
    import_qb_report,
)

FIXTURES = Path(__file__).parent / "fixtures"
PLAIN = (FIXTURES / "qb_transaction_detail_invoices.csv").read_text(encoding="utf-8")
WITH_OPEN = (FIXTURES / "qb_transaction_detail_invoices_open_balance.csv").read_text(
    encoding="utf-8"
)
RECEIPTS = (FIXTURES / "qb_transaction_detail_sales_receipts.csv").read_text(
    encoding="utf-8"
)


def _items(client, seed_accounts):
    for name, rate in (("Exterior door", "400.00"), ("Install labor", "100.00")):
        r = client.post(
            "/api/items",
            json={
                "name": name,
                "item_type": "service",
                "rate": rate,
                "income_account_id": seed_accounts["4000"].id,
            },
        )
        assert r.status_code == 201, r.text


def test_detection_tells_invoices_from_receipts():
    assert detect_report_type(PLAIN) == "invoices"
    assert detect_report_type(WITH_OPEN) == "sales_and_invoices"
    assert detect_report_type(RECEIPTS) == "sales_receipts"


def test_invoices_land_with_their_lines_jobs_and_numbers(
    client, db_session, seed_accounts
):
    from app.models.invoices import Invoice, InvoiceStatus
    from app.models.jobs import Job

    _items(client, seed_accounts)
    result = import_invoice_report(db_session, PLAIN)
    assert result["errors"] == [], result
    assert result["imported"] == 3 and result["open"] == 3
    assert any("no Open Balance column" in w for w in result["warnings"])
    by_num = {i.invoice_number: i for i in db_session.query(Invoice).all()}
    assert set(by_num) == {"1031", "1032", "1033"}
    one = by_num["1031"]
    assert one.customer.name == "Alex Vega"
    job = db_session.get(Job, one.job_id)
    assert (
        job is not None
        and job.name == "Kitchen remodel"
        and job.customer_id == one.customer_id
    )
    lines = sorted(one.lines, key=lambda ln: ln.line_order)
    assert [
        (
            ln.item.name if ln.item else None,
            float(ln.quantity),
            float(ln.rate),
            float(ln.amount),
        )
        for ln in lines
    ] == [
        ("Exterior door", 3.0, 400.0, 1200.0),
        ("Install labor", 1.5, 100.0, 150.0),
    ]
    assert float(one.total) == 1350.0 and one.status == InvoiceStatus.SENT
    assert float(one.balance_due) == 1350.0 and float(one.amount_paid) == 0
    two = by_num["1032"]
    assert (
        float(two.tax_amount) == 37.5
        and float(two.tax_rate) == 0.075
        and float(two.total) == 537.5
    )
    assert not any(i.is_sales_receipt for i in by_num.values())
    # the journals: DR A/R the total, CR income and tax
    for inv in by_num.values():
        assert inv.transaction_id is not None


def test_open_balance_sets_paid_partly_paid_and_open(client, db_session, seed_accounts):
    from app.models.invoices import Invoice, InvoiceStatus
    from app.models.payments import Payment, PaymentAllocation
    from app.models.transactions import TransactionLine

    _items(client, seed_accounts)
    result = import_qb_report(db_session, WITH_OPEN)
    assert result["errors"] == [], result
    assert result["detected"] == "sales_and_invoices"
    assert result["invoices"] == 3 and result["sales_receipts"] == 1
    assert (
        result["invoices_paid"],
        result["invoices_partly_paid"],
        result["invoices_open"],
    ) == (1, 1, 1)
    assert not any("no Open Balance" in w for w in result["warnings"])
    by_num = {
        i.invoice_number: i
        for i in db_session.query(Invoice).filter_by(is_sales_receipt=False).all()
    }
    paid, part, open_ = by_num["1031"], by_num["1032"], by_num["1033"]
    assert (
        paid.status == InvoiceStatus.PAID
        and float(paid.balance_due) == 0
        and float(paid.amount_paid) == 1350.0
    )
    assert (
        part.status == InvoiceStatus.PARTIAL
        and float(part.balance_due) == 237.5
        and float(part.amount_paid) == 300.0
    )
    assert open_.status == InvoiceStatus.SENT and float(open_.balance_due) == 80.0
    pays = {a.invoice_id: a for a in db_session.query(PaymentAllocation).all()}
    assert (
        float(pays[paid.id].amount) == 1350.0 and float(pays[part.id].amount) == 300.0
    )
    assert open_.id not in pays
    for p in db_session.query(Payment).all():
        inv_id = next(a.invoice_id for a in pays.values() if a.payment_id == p.id)
        assert p.date == db_session.get(Invoice, inv_id).date
        assert p.deposit_to_account_id == seed_accounts["1200"].id
    # every journal balances
    for t_id in {ln.transaction_id for ln in db_session.query(TransactionLine).all()}:
        rows = db_session.query(TransactionLine).filter_by(transaction_id=t_id).all()
        assert sum(r.debit for r in rows) == sum(r.credit for r in rows)
    # the receipt in the same file came in as a receipt, once
    receipts = db_session.query(Invoice).filter_by(is_sales_receipt=True).all()
    assert len(receipts) == 1 and receipts[0].invoice_number == "7"


def test_a_reupload_skips_what_is_here(client, db_session, seed_accounts):
    _items(client, seed_accounts)
    first = import_qb_report(db_session, WITH_OPEN)
    again = import_qb_report(db_session, WITH_OPEN)
    assert first["invoices"] == 3 and first["sales_receipts"] == 1
    assert again["invoices"] == 0 and again["sales_receipts"] == 0
    assert again["duplicates_skipped"] == 4


def test_the_endpoint_detects_an_invoice_report(client, seed_accounts):
    import io

    _items(client, seed_accounts)
    r = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("invoices.csv", io.BytesIO(PLAIN.encode("utf-8")), "text/csv")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected"] == "invoices" and body["invoices"] == 3
    assert body["invoices_open"] == 3
    assert body["sales_receipts"] == 0
    assert any("no Open Balance" in w for w in body["warnings"])


def test_the_receipt_importer_leaves_invoice_blocks_alone(db_session, seed_accounts):
    from app.models.invoices import Invoice
    from app.services.qb_report_import import import_sales_receipt_report

    result = import_sales_receipt_report(db_session, WITH_OPEN)
    assert result["imported"] == 1 and result["errors"] == []
    assert db_session.query(Invoice).count() == 1  # the receipt only


def test_a_line_sum_that_misses_the_total_is_refused_whole(db_session, seed_accounts):
    broken = PLAIN.replace('"1,350.00",,0.00,0.00', '"1,400.00",,0.00,0.00', 1)
    result = import_invoice_report(db_session, broken)
    assert result["imported"] == 2
    assert any("Invoice 1031" in e and "do not equal" in e for e in result["errors"])


def test_the_page_names_invoices(client):
    js = (
        Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "iif.js"
    ).read_text(encoding="utf-8")
    assert "invoices: 'Invoices'" in js and "sales_and_invoices" in js
    assert "Receipt or Invoice (a receipt imports as a paid sale + payment" in js


def test_decimal_boundaries_hold(db_session, seed_accounts):
    """Amounts quantize to cents and the tax rate keeps its places."""
    result = import_invoice_report(db_session, PLAIN)
    assert result["imported"] == 3
    from app.models.invoices import Invoice

    inv = db_session.query(Invoice).filter_by(invoice_number="1032").one()
    assert inv.tax_rate == Decimal("0.075000") and inv.tax_amount == Decimal("37.50")


def test_the_ledger_says_the_documents_face_and_the_messages_the_companys_word(
    client, db_session, seed_accounts
):
    """What an imported invoice posts reads as one made here does ("Invoice
    #1031", the document's own face); the import's messages say the
    company's word for the report's document, so a nonprofit reads Pledge
    (test_document_references' sweep)."""
    from app.models.invoices import Invoice
    from app.models.transactions import Transaction

    assert import_invoice_report(db_session, PLAIN)["imported"] == 3
    inv = db_session.query(Invoice).filter_by(invoice_number="1031").one()
    txn = db_session.get(Transaction, inv.transaction_id)
    assert txn.description == "QB report import — Invoice #1031"
    # the receivable line names the document; the income lines keep the items' words
    assert [ln.description for ln in txn.lines if ln.debit] == ["Invoice #1031"]

    client.put("/api/settings", json={"company_type": "nonprofit"})
    broken = PLAIN.replace('"1,350.00",,0.00,0.00', '"1,400.00",,0.00,0.00', 1)
    result = import_invoice_report(db_session, broken)
    assert (
        result["imported"] == 0
    )  # the three are here already: 1031 refused, two skipped
    assert any(
        e.startswith("Pledge 1031:") and "the pledge total" in e
        for e in result["errors"]
    ), result["errors"]


# ---- the same source document (2.23.0 gate, W-2 / NEW-87, NEW-84) ----


def _renumber(db_session, invoice_id, number):
    """The API numbers documents itself; the gate's companies carry these
    numbers from their own history, so give it to the row."""
    from app.models.invoices import Invoice

    inv = db_session.get(Invoice, invoice_id)
    inv.invoice_number = number
    db_session.commit()


def _invoice_here(client, db_session, customer_name, number, total="99.00"):
    cust = client.post("/api/customers", json={"name": customer_name})
    assert cust.status_code == 201, cust.text
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": cust.json()["id"],
            "date": "2026-03-01",
            "lines": [{"description": "work", "quantity": 1, "rate": total}],
        },
    )
    assert r.status_code == 201, r.text
    _renumber(db_session, r.json()["id"], number)
    return r.json()


def test_another_customers_invoice_under_the_same_number_is_not_a_duplicate(
    client, db_session, seed_accounts
):
    """The number alone said "already here" when any invoice here carried
    1031; the report's 1031 is Alex Vega's, this one is someone else's."""
    from app.models.invoices import Invoice

    _items(client, seed_accounts)
    _invoice_here(client, db_session, "Garland Theater", "1031")
    result = import_qb_report(db_session, WITH_OPEN)
    assert result["invoices"] == 3 and result["duplicates_skipped"] == 0, result
    mine = db_session.query(Invoice).filter_by(invoice_number="QB-1031").one()
    assert mine.customer.name == "Alex Vega" and float(mine.total) == 1350.0
    theirs = db_session.query(Invoice).filter_by(invoice_number="1031").one()
    assert theirs.customer.name == "Garland Theater"


def test_a_renumbered_import_is_found_again_on_the_next_upload(
    client, db_session, seed_accounts
):
    """A sales receipt here held 1031, so the import came in as QB-1031 —
    and every upload after that imported it again (QB-1031-1, -2 …) with
    another payment each time."""
    from app.models.invoices import Invoice
    from app.models.payments import Payment

    _items(client, seed_accounts)
    r = client.post(
        "/api/sales-receipts",
        json={
            "customer_id": client.post(
                "/api/customers", json={"name": "Walk-in"}
            ).json()["id"],
            "date": "2026-02-02",
            "method": "Cash",
            "lines": [{"description": "cash sale", "quantity": 1, "rate": "5.00"}],
        },
    )
    assert r.status_code == 201, r.text
    _renumber(db_session, r.json()["invoice"]["id"], "1031")
    first = import_qb_report(db_session, WITH_OPEN)
    assert first["invoices"] == 3 and first["duplicates_skipped"] == 0, first
    assert db_session.query(Invoice).filter_by(invoice_number="QB-1031").count() == 1
    payments = db_session.query(Payment).count()
    again = import_qb_report(db_session, WITH_OPEN)
    assert again["invoices"] == 0 and again["invoices_updated"] == 0, again
    assert again["duplicates_skipped"] == 4  # three invoices and the receipt
    assert (
        db_session.query(Invoice)
        .filter(Invoice.invoice_number.like("QB-1031%"))
        .count()
        == 1
    )
    assert db_session.query(Payment).count() == payments


def test_an_open_balance_upload_marks_the_open_ones_paid(
    client, db_session, seed_accounts
):
    """Without the column every invoice came in open; uploading the report
    again with it, the ones with nothing paid on them take their payment
    (the warning says so). One with no open balance owed stays as it is."""
    from app.models.invoices import Invoice, InvoiceStatus
    from app.models.payments import Payment

    _items(client, seed_accounts)
    plain = import_qb_report(db_session, PLAIN)
    assert plain["invoices"] == 3 and plain["invoices_open"] == 3
    assert any("marked paid or partly paid" in w for w in plain["warnings"])
    then = import_qb_report(db_session, WITH_OPEN)
    assert then["invoices"] == 0 and then["sales_receipts"] == 1, then
    assert then["invoices_updated"] == 2  # 1031 paid, 1032 partly paid
    assert then["duplicates_skipped"] == 1  # 1033 is open in the report too
    by_num = {i.invoice_number: i for i in db_session.query(Invoice).all()}
    assert by_num["1031"].status == InvoiceStatus.PAID
    assert float(by_num["1031"].amount_paid) == 1350.0
    assert by_num["1032"].status == InvoiceStatus.PARTIAL
    assert (float(by_num["1032"].amount_paid), float(by_num["1032"].balance_due)) == (
        300.0,
        237.5,
    )
    assert by_num["1033"].status == InvoiceStatus.SENT
    paid = (
        db_session.query(Payment)
        .filter(Payment.deposit_to_account_id.isnot(None))
        .all()
    )
    assert sorted(float(p.amount) for p in paid if p.reference in ("1031", "1032")) == [
        300.0,
        1350.0,
    ]
    assert all(p.transaction_id for p in paid)
    # a third upload changes nothing
    third = import_qb_report(db_session, WITH_OPEN)
    assert (
        third["invoices"],
        third["invoices_updated"],
        third["duplicates_skipped"],
    ) == (
        0,
        0,
        4,
    )
    assert db_session.query(Payment).count() == len(paid)


def test_an_invoice_with_a_payment_on_it_is_left_alone(
    client, db_session, seed_accounts
):
    from app.models.invoices import Invoice
    from app.models.payments import Payment

    _items(client, seed_accounts)
    import_qb_report(db_session, PLAIN)
    inv = db_session.query(Invoice).filter_by(invoice_number="1031").one()
    r = client.post(
        "/api/payments",
        json={
            "customer_id": inv.customer_id,
            "date": "2026-05-01",
            "amount": "100.00",
            "method": "Check",
            "allocations": [{"invoice_id": inv.id, "amount": "100.00"}],
        },
    )
    assert r.status_code == 201, r.text
    then = import_qb_report(db_session, WITH_OPEN)
    assert then["invoices_updated"] == 1  # 1032 only
    db_session.expire_all()
    inv = db_session.query(Invoice).filter_by(invoice_number="1031").one()
    assert float(inv.amount_paid) == 100.0
    assert db_session.query(Payment).filter_by(reference="1031").count() == 0


def test_the_no_open_balance_notice_counts_what_came_in_and_is_silent_otherwise(
    client, db_session, seed_accounts
):
    """A re-upload that skipped every invoice still said "every invoice came
    in open" (2.23.0 gate, NEW-92)."""
    _items(client, seed_accounts)
    first = import_qb_report(db_session, PLAIN)
    notices = [w for w in first["warnings"] if "no Open Balance column" in w]
    assert len(notices) == 1 and "the 3 invoices that came in are open" in notices[0]
    again = import_qb_report(db_session, PLAIN)
    assert again["invoices"] == 0 and again["duplicates_skipped"] == 3
    assert not any("Open Balance" in w for w in again["warnings"]), again["warnings"]


def test_the_no_open_balance_notice_speaks_the_companys_words_throughout(
    client, db_session, seed_accounts
):
    """For a nonprofit the sentence counted pledges and then said "an
    invoice here" (2.23.0 gate, W-7)."""
    _items(client, seed_accounts)
    client.put("/api/settings", json={"company_type": "nonprofit"})
    result = import_qb_report(db_session, PLAIN)
    notice = next(w for w in result["warnings"] if "no Open Balance column" in w)
    assert "the 3 pledges that came in are open" in notice
    assert "a pledge here with nothing paid on it" in notice
    assert "invoice" not in notice.lower(), notice
