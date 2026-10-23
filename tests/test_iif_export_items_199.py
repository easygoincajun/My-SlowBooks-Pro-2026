"""The IIF export keeps an invoice's job, items and quantities (#199).

It wrote the customer without the job and split rows with no item,
quantity or price, so an invoice that went out and came back (into
QuickBooks, or back into SlowBooks) arrived with no job, no item and a
quantity of 1. Per Intuit's own sample import files: the TRNS row's NAME
is the full Customer:Job; INVITEM, QNTY, PRICE and TAXABLE sit on the
!SPL rows only, the quantity negative and the price positive on a sale,
AMOUNT = QNTY × PRICE.
"""

import io


def _post(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 201, (path, r.text)
    return r.json()


def _books(client, seed_accounts):
    cust = _post(client, "/api/customers", {"name": "Bob Jones"})
    job = _post(
        client, "/api/jobs", {"customer_id": cust["id"], "name": "Kitchen remodel"}
    )
    income = seed_accounts["4000"].id
    door = _post(
        client,
        "/api/items",
        {
            "name": "Exterior door",
            "item_type": "product",
            "rate": "25.00",
            "income_account_id": income,
        },
    )
    labor = _post(
        client,
        "/api/items",
        {
            "name": "Install labor",
            "item_type": "labor",
            "rate": "40.00",
            "income_account_id": income,
            "is_taxable": False,
        },
    )
    return cust, job, door, labor


def _lines(text, kind):
    return [
        ln
        for ln in text.replace("\r\n", "\n").split("\n")
        if ln.startswith(kind + "\t")
    ]


def test_the_invoice_export_names_the_job_and_the_items(client, seed_accounts):
    cust, job, door, labor = _books(client, seed_accounts)
    inv = _post(
        client,
        "/api/invoices",
        {
            "customer_id": cust["id"],
            "job_id": job["id"],
            "date": "2026-09-10",
            "tax_rate": "0.10",
            "lines": [
                {
                    "item_id": door["id"],
                    "description": "Exterior door",
                    "quantity": "3",
                    "rate": "25.00",
                },
                {
                    "item_id": labor["id"],
                    "description": "Install",
                    "quantity": "1.5",
                    "rate": "40.00",
                    "is_taxable": False,
                },
            ],
        },
    )
    text = client.get("/api/iif/export/invoices").text
    header = [
        ln for ln in text.replace("\r\n", "\n").split("\n") if ln.startswith("!SPL\t")
    ][0]
    assert header.endswith("\tCLASS\tINVITEM\tQNTY\tPRICE\tTAXABLE")
    trns_header = [
        ln for ln in text.replace("\r\n", "\n").split("\n") if ln.startswith("!TRNS\t")
    ][0]
    assert "INVITEM" not in trns_header  # on the split rows only
    [trns] = _lines(text, "TRNS")
    assert "\tBob Jones:Kitchen remodel\t" in trns
    spl = _lines(text, "SPL")
    assert len(spl) == 3  # two items and the tax
    assert spl[0].endswith("\tExterior door\t-3\t25\tY") and "\t-75.00\t" in spl[0]
    assert spl[1].endswith("\tInstall labor\t-1.5\t40\tN") and "\t-60.00\t" in spl[1]
    assert spl[2].endswith("\tSales Tax\t\t\t\t\t") and "\t-7.50\t" in spl[2]
    assert float(inv["total"]) == 142.5


def test_the_round_trip_keeps_the_job_the_items_and_the_quantities(
    client, db_session, seed_accounts
):
    from app.models.invoices import Invoice

    cust, job, door, labor = _books(client, seed_accounts)
    first = _post(
        client,
        "/api/invoices",
        {
            "customer_id": cust["id"],
            "job_id": job["id"],
            "date": "2026-09-10",
            "tax_rate": "0.10",
            "lines": [
                {
                    "item_id": door["id"],
                    "description": "Exterior door",
                    "quantity": "3",
                    "rate": "25.00",
                },
                {
                    "item_id": labor["id"],
                    "description": "Install",
                    "quantity": "1.5",
                    "rate": "40.00",
                    "is_taxable": False,
                },
            ],
        },
    )
    text = client.get("/api/iif/export/invoices").text
    # the same document under a new number, so the import's dedupe lets it in
    again = text.replace(f"\t{first['invoice_number']}\t", "\tRT-199\t")
    r = client.post(
        "/api/iif/import",
        files={"file": ("export.iif", io.BytesIO(again.encode("utf-8")), "text/plain")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["invoices"] == 1 and r.json()["errors"] == []
    db_session.expire_all()
    back = db_session.query(Invoice).filter_by(invoice_number="RT-199").one()
    assert back.customer_id == cust["id"] and back.job_id == job["id"]
    lines = sorted(back.lines, key=lambda ln: float(ln.rate))
    assert [(ln.item_id, float(ln.quantity), float(ln.rate)) for ln in lines] == [
        (door["id"], 3.0, 25.0),
        (labor["id"], 1.5, 40.0),
    ]
    assert float(back.total) == float(first["total"]) == 142.5
    assert float(back.tax_amount) == 7.5
    # the labor line went out TAXABLE N and comes back so (it came back
    # taxable, the line's default, so a re-tax on the edit form taxed it)
    assert [ln.is_taxable for ln in lines] == [True, False]


def test_a_sales_receipt_carries_its_items_too(client, seed_accounts):
    cust, job, door, labor = _books(client, seed_accounts)
    _post(
        client,
        "/api/sales-receipts",
        {
            "customer_id": cust["id"],
            "date": "2026-09-11",
            "tax_rate": "0",
            "method": "Cash",
            "lines": [
                {
                    "item_id": door["id"],
                    "description": "Exterior door",
                    "quantity": "2",
                    "rate": "25.00",
                    "line_order": 0,
                }
            ],
        },
    )
    text = client.get("/api/iif/export/sales-receipts").text
    header = [
        ln for ln in text.replace("\r\n", "\n").split("\n") if ln.startswith("!SPL\t")
    ][0]
    assert header.endswith("\tINVITEM\tQNTY\tPRICE\tTAXABLE")
    [spl] = _lines(text, "SPL")
    assert spl.endswith("\tExterior door\t-2\t25\tY") and "\t-50.00\t" in spl


def test_a_document_in_another_currency_prices_its_lines_in_home_currency(
    client, db_session, seed_accounts
):
    """Every amount in the file is home currency (the ledger's); the unit
    price was the typed one in the document's currency, so AMOUNT was not
    QNTY × PRICE on those rows (2.23.0 gate, W-4 / NEW-90)."""
    from decimal import Decimal

    from app.models.invoices import Invoice

    cust, job, door, labor = _books(client, seed_accounts)
    inv = _post(
        client,
        "/api/invoices",
        {
            "customer_id": cust["id"],
            "date": "2026-09-12",
            "tax_rate": "0",
            "lines": [
                {
                    "item_id": door["id"],
                    "description": "Widget",
                    "quantity": "3",
                    "rate": "33.33",
                }
            ],
        },
    )
    row = db_session.get(Invoice, inv["id"])
    row.currency, row.exchange_rate = "EUR", Decimal("1.2345")
    db_session.commit()
    text = client.get("/api/iif/export/invoices").text
    [spl] = _lines(text, "SPL")
    cols = spl.split("\t")
    amount, qnty, price = Decimal(cols[5]), Decimal(cols[-3]), Decimal(cols[-2])
    assert amount == Decimal("-123.44")  # 99.99 EUR at 1.2345, as the ledger books it
    assert (qnty, price) == (
        Decimal("-3"),
        Decimal("41.1467"),
    )  # 123.44 ÷ 3, four places
    assert (qnty * price).quantize(Decimal("0.01")) == amount
    # a home-currency row keeps the typed price
    assert _item_fields_plain(client, seed_accounts)


def _item_fields_plain(client, seed_accounts):
    from decimal import Decimal

    from app.services.iif_export import _item_fields

    class Line:
        quantity, rate, item, is_taxable = Decimal("2"), Decimal("25.50"), None, True

    return _item_fields(Line()) == ["", "-2", "25.5", "Y"]


def test_a_large_quantity_gets_the_places_it_needs(client, db_session, seed_accounts):
    """Round 2's PRICE was the rate converted and rounded to four places:
    1,000 × 0.3333 EUR at 1.2345 wrote PRICE 0.4115 beside AMOUNT -411.46,
    and 1,000 × 0.4115 is 411.50 (2.23.0 gate, W-6 / NEW-93). The price
    now comes from the booked amount, to the places the quantity needs."""
    from decimal import Decimal

    from app.models.invoices import Invoice

    cust, job, door, labor = _books(client, seed_accounts)
    inv = _post(
        client,
        "/api/invoices",
        {
            "customer_id": cust["id"],
            "date": "2026-09-13",
            "tax_rate": "0",
            "lines": [
                {
                    "item_id": door["id"],
                    "description": "Washers",
                    "quantity": "1000",
                    "rate": "0.3333",
                },
                {
                    "item_id": labor["id"],
                    "description": "Seven",
                    "quantity": "7",
                    "rate": "1.111",
                    "is_taxable": False,
                },
            ],
        },
    )
    row = db_session.get(Invoice, inv["id"])
    row.currency, row.exchange_rate = "EUR", Decimal("1.2345")
    db_session.commit()
    text = client.get("/api/iif/export/invoices").text
    rows = [ln for ln in _lines(text, "SPL") if f"\t{inv['invoice_number']}\t" in ln]
    seen = {}
    for ln in rows:
        cols = ln.split("\t")
        amount, qnty, price = Decimal(cols[5]), Decimal(cols[-3]), Decimal(cols[-2])
        seen[qnty] = (amount, price)
        assert (qnty * price).quantize(Decimal("0.01")) == amount, ln
    assert seen[Decimal("-1000")] == (Decimal("-411.46"), Decimal("0.41146"))
    assert seen[Decimal("-7")] == (Decimal("-9.60"), Decimal("1.3714"))
