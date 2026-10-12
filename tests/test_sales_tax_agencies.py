"""Sales tax split between agencies (app/services/sales_tax_agencies.py).

Louisiana files state and parish sales tax separately. A document keeps one
combined rate; with agencies set up in Settings, the tax it collects posts
to each agency's account in proportion to its rate (the last takes the
rounding cent), credit memos take it back the same way, the Sales Tax
report shows each agency, and a payment settles one. With none set up,
everything posts to 2200 as before.
"""

from decimal import Decimal

from app.models.accounts import Account
from app.models.invoices import Invoice
from app.services.sales_tax_agencies import split_tax

PERIOD = "start_date=2026-07-01&end_date=2026-07-31"
STATE, PARISH = "Louisiana State", "Jefferson Parish"


def _agencies(client, *rows):
    r = client.put("/api/settings/sales-tax-agencies", json=list(rows))
    assert r.status_code == 200, r.text
    return {a["name"]: a for a in r.json()}


def _split(client):
    return _agencies(
        client, {"name": STATE, "rate": "5"}, {"name": PARISH, "rate": "5.25"}
    )


def _invoice(client, customer_id, rate=400, tax_rate=0.1025):
    item = client.post(
        "/api/items",
        json={
            "name": f"Router {rate}",
            "item_type": "product",
            "rate": rate,
            "is_taxable": True,
        },
    ).json()
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": customer_id,
            "date": "2026-07-02",
            "tax_rate": tax_rate,
            "lines": [{"item_id": item["id"], "quantity": 1, "rate": rate}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _txn(db_session, inv):
    return db_session.get(Invoice, inv["id"]).transaction


def _tax_lines(db_session, inv, names):
    txn = _txn(db_session, inv)
    by_id = {a.id: a.name for a in db_session.query(Account)}
    return {
        by_id[ln.account_id]: ln for ln in txn.lines if by_id[ln.account_id] in names
    }


def test_without_agencies_tax_goes_to_2200(client, db_session, seed_accounts):
    acct = seed_accounts["2200"].id
    assert split_tax(db_session, Decimal("41.00"), acct) == [
        (acct, Decimal("41.00"), None)
    ]


def test_the_split_is_in_proportion_and_adds_up(client, db_session, seed_accounts):
    made = _split(client)
    parts = split_tax(db_session, Decimal("10.01"), seed_accounts["2200"].id)
    assert [p[1] for p in parts] == [
        Decimal("4.88"),
        Decimal("5.13"),
    ]  # last takes the cent
    assert [p[0] for p in parts] == [
        made[STATE]["account_id"],
        made[PARISH]["account_id"],
    ]


def test_saving_agencies_makes_their_accounts_and_the_default_rate(
    client, db_session, seed_accounts
):
    made = _split(client)
    assert made[STATE]["account_name"] == f"Sales Tax Payable – {STATE}"
    assert client.get("/api/settings").json()["default_tax_rate"] == "10.25"
    # an existing liability account can be one of them
    made = _agencies(
        client,
        {"name": STATE, "rate": "5", "account_id": seed_accounts["2200"].id},
        {"name": PARISH, "rate": "5.25"},
    )
    assert made[STATE]["account_name"] == "Sales Tax Payable"
    # and none goes back to one rate on 2200
    assert _agencies(client) == {}


def test_bad_agencies_are_refused(client, seed_accounts):
    for rows in (
        [{"name": STATE, "rate": "0"}],
        [{"name": STATE, "rate": "5"}, {"name": STATE.upper(), "rate": "1"}],
        [{"name": STATE, "rate": "5", "account_id": seed_accounts["1000"].id}],
        [{"name": " ", "rate": "5"}],
    ):
        r = client.put("/api/settings/sales-tax-agencies", json=rows)
        assert r.status_code == 400, (rows, r.text)


def test_an_invoice_posts_each_agencys_share(
    client, db_session, seed_accounts, seed_customer
):
    _split(client)
    inv = _invoice(client, seed_customer.id)
    assert Decimal(str(inv["tax_amount"])) == Decimal("41.00")
    lines = _tax_lines(
        db_session,
        inv,
        {f"Sales Tax Payable – {STATE}", f"Sales Tax Payable – {PARISH}"},
    )
    assert lines[f"Sales Tax Payable – {STATE}"].credit == Decimal("20.00")
    assert lines[f"Sales Tax Payable – {PARISH}"].credit == Decimal("21.00")
    assert lines[f"Sales Tax Payable – {PARISH}"].description == f"Sales tax: {PARISH}"
    txn = _txn(db_session, inv)
    assert sum(ln.debit for ln in txn.lines) == sum(ln.credit for ln in txn.lines)


def test_a_credit_memo_takes_each_share_back(
    client, db_session, seed_accounts, seed_customer
):
    _split(client)
    inv = _invoice(client, seed_customer.id)
    r = client.post(
        "/api/credit-memos",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-10",
            "tax_rate": 0.1025,
            "original_invoice_id": inv["id"],
            "lines": [{"description": "Returned router", "quantity": 1, "rate": 400}],
        },
    )
    assert r.status_code == 201, r.text
    for name in (STATE, PARISH):
        acct = (
            db_session.query(Account)
            .filter(Account.name == f"Sales Tax Payable – {name}")
            .one()
        )
        assert acct.balance == Decimal("0")


def test_the_report_shows_each_agency_and_agrees(client, seed_accounts, seed_customer):
    _split(client)
    _invoice(client, seed_customer.id)
    ledger = client.get(f"/api/reports/sales-tax?{PERIOD}").json()["ledger"]
    assert ledger["difference"] == 0
    by_name = {a["name"]: a for a in ledger["agencies"]}
    assert (by_name[STATE]["rate"], by_name[STATE]["tax_posted"]) == (5.0, 20.0)
    assert (by_name[PARISH]["tax_posted"], by_name[PARISH]["balance"]) == (21.0, 21.0)


def test_a_payment_settles_one_agency(client, db_session, seed_accounts, seed_customer):
    made = _split(client)
    _invoice(client, seed_customer.id)
    r = client.post(
        "/api/reports/sales-tax/pay",
        json={
            "date": "2026-07-20",
            "amount": 21,
            "pay_from_account_id": seed_accounts["1000"].id,
            "tax_account_id": made[PARISH]["account_id"],
        },
    )
    assert r.status_code == 200, r.text
    by_name = {
        a["name"]: a
        for a in client.get(f"/api/reports/sales-tax?{PERIOD}").json()["ledger"][
            "agencies"
        ]
    }
    assert (by_name[PARISH]["payments"], by_name[PARISH]["balance"]) == (21.0, 0.0)
    assert by_name[STATE]["balance"] == 20.0
    # only a sales tax account can be paid this way
    r = client.post(
        "/api/reports/sales-tax/pay",
        json={
            "amount": 5,
            "pay_from_account_id": seed_accounts["1000"].id,
            "tax_account_id": seed_accounts["2000"].id,
        },
    )
    assert r.status_code == 400
