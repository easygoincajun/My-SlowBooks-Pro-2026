"""A negative amount prints "-$10.00" on a document, not "$-10.00" (2.18.0).

A discount line is a negative line, and the money filters every document
template shares put the minus after the dollar sign: "$-10.00" on the
invoice PDF, its print preview and the sales receipt. The filters now put
the sign first, as the app's pages do (formatCurrency: "-$10.00"); every
positive amount prints as it did.
"""

from decimal import Decimal

from app.models.items import Item, ItemType
from app.services import pdf_service


def test_the_money_filters_put_the_minus_before_the_dollar_sign():
    assert pdf_service._format_currency(-10) == "-$10.00"
    assert pdf_service._format_currency(Decimal("-1234.5")) == "-$1,234.50"
    assert pdf_service._format_currency(-10, "EUR") == "EUR -10.00"
    assert pdf_service._format_rate(Decimal("-10.0000")) == "-$10.00"
    assert pdf_service._format_rate(Decimal("-0.0450")) == "-$0.045"
    assert pdf_service._format_rate(Decimal("-0.0450"), "CAD") == "CAD -0.045"
    # an amount that rounds to nothing has no sign
    assert pdf_service._format_currency(-0.004) == "$0.00"


def test_every_positive_amount_prints_as_it_did():
    assert [
        pdf_service._format_currency(v)
        for v in (0, None, 850, Decimal("1234.5"), 10824891.75)
    ] == ["$0.00", "$0.00", "$850.00", "$1,234.50", "$10,824,891.75"]
    assert pdf_service._format_currency(850, "EUR") == "EUR 850.00"
    assert [
        pdf_service._format_rate(Decimal(v)) for v in ("12.5000", "0.0450", "1234.5")
    ] == ["$12.50", "$0.045", "$1,234.50"]


def _discount_item(db):
    """An item a discount came in on (services/qbo_common.py): a line on it
    may carry a negative price."""
    from app.services.qbo_common import create_mapping

    item = Item(name="Discount", item_type=ItemType.SERVICE, is_active=True)
    db.add(item)
    db.flush()
    create_mapping(db, "discount_item", item.id, "")
    db.commit()
    return item


def test_a_discount_prints_with_its_minus_first(
    client, db_session, seed_accounts, seed_customer
):
    item = _discount_item(db_session)
    lines = [
        {"description": "Catering", "quantity": 1, "rate": 100, "is_taxable": True},
        {
            "item_id": item.id,
            "description": "Discount",
            "quantity": 1,
            "rate": -10,
            "is_taxable": True,
        },
    ]
    invoice = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-08-03",
            "tax_rate": "0.089",
            "lines": lines,
        },
    )
    assert invoice.status_code == 201, invoice.text
    receipt = client.post(
        "/api/sales-receipts",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-08-03",
            "tax_rate": "0.089",
            "method": "Cash",
            "lines": [{**line, "line_order": n} for n, line in enumerate(lines)],
        },
    )
    assert receipt.status_code == 201, receipt.text
    for document in (invoice.json(), receipt.json()["invoice"]):
        assert Decimal(str(document["total"])) == Decimal("98.01"), document
        html = client.get(f"/api/invoices/{document['id']}/print-preview").text
        assert html.count("-$10.00") == 2, document["id"]  # its price and its amount
        assert "$-" not in html
        assert "$98.01" in html
