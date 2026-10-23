"""The customer's credit limit, told to every way of saving an invoice
(#256, 2.22.2).

The warning lived in the invoice form only: `credit_limit` is stored on the
customer and imported from QuickBooks, and no Python read it, so
`POST /api/invoices` saved an over-limit invoice silently, though the
product's own pitch is that agents use this API. Now a document that would
take the customer past their limit is refused with 409 "credit_limit",
naming the limit and what they'd owe, unless the request says
`allow_over_credit_limit: true`; the page asks the question and sends it
again with the flag. What they owe counts the credits they hold.
"""

from decimal import Decimal

import pytest

from app.models.invoices import Invoice

D = Decimal


def _lines(rate):
    return [{"description": "Service", "quantity": 1, "rate": str(rate)}]


def _post(client, customer_id, rate, **extra):
    return client.post(
        "/api/invoices",
        json={
            "customer_id": customer_id,
            "date": "2026-10-01",
            "lines": _lines(rate),
            **extra,
        },
    )


@pytest.fixture
def limited(client, seed_accounts):
    """A customer with a $500 limit who already owes $300."""
    c = client.post(
        "/api/customers",
        json={"name": "Salt & Pine Catering Co.", "credit_limit": "500"},
    ).json()
    r = _post(client, c["id"], "300")
    assert r.status_code == 201, r.text
    return c


def test_an_invoice_past_the_limit_is_refused_with_the_question(
    client, db_session, limited
):
    before = db_session.query(Invoice).count()
    r = _post(client, limited["id"], "250")
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "credit_limit"
    assert d["message"] == (
        "Salt & Pine Catering Co.'s credit limit is $500.00. With this invoice they would owe $550.00."
    )
    assert d["question"].endswith("Save it anyway?")
    assert (d["limit"], d["would_owe"]) == ("500.00", "550.00")
    assert db_session.query(Invoice).count() == before  # nothing written
    # the same request with the flag saves
    r = _post(client, limited["id"], "250", allow_over_credit_limit=True)
    assert r.status_code == 201, r.text


def test_at_the_limit_is_fine_and_a_credit_they_hold_counts(client, limited):
    assert _post(client, limited["id"], "200").status_code == 201  # exactly $500
    # a $100 credit memo brings what they owe to $400, so $100 more fits
    r = client.post(
        "/api/credit-memos",
        json={
            "customer_id": limited["id"],
            "date": "2026-10-01",
            "lines": _lines("100"),
        },
    )
    assert r.status_code == 201, r.text
    assert _post(client, limited["id"], "100").status_code == 201
    assert _post(client, limited["id"], "0.01").status_code == 409


def test_an_edit_that_takes_them_past_the_limit_asks_and_one_that_does_not_is_silent(
    client, limited
):
    inv = _post(client, limited["id"], "150").json()  # owes 450
    r = client.put(
        f"/api/invoices/{inv['id']}", json={"lines": _lines("250")}
    )  # would owe 550
    assert r.status_code == 409 and r.json()["detail"]["code"] == "credit_limit"
    r = client.put(f"/api/invoices/{inv['id']}", json={"notes": "called twice"})
    assert r.status_code == 200, r.text  # nothing about the amount changed
    r = client.put(
        f"/api/invoices/{inv['id']}",
        json={"lines": _lines("250"), "allow_over_credit_limit": True},
    )
    assert r.status_code == 200, r.text
    # and lowering it back needs no say-so, though they are over the limit
    r = client.put(f"/api/invoices/{inv['id']}", json={"lines": _lines("100")})
    assert r.status_code == 200, r.text


def test_a_sales_receipt_and_a_customer_with_no_limit_are_never_asked(
    client, seed_accounts, limited
):
    # paid at once: nothing is owed
    r = client.post(
        "/api/sales-receipts",
        json={
            "customer_id": limited["id"],
            "date": "2026-10-01",
            "lines": _lines("5000"),
        },
    )
    assert r.status_code == 201, r.text
    free = client.post("/api/customers", json={"name": "No Limit Co."}).json()
    assert _post(client, free["id"], "1000000").status_code == 201


def test_converting_an_estimate_past_the_limit_asks(client, limited):
    r = client.post(
        "/api/estimates",
        json={
            "customer_id": limited["id"],
            "date": "2026-10-01",
            "lines": _lines("250"),
        },
    )
    assert r.status_code == 201, r.text
    est = r.json()
    r = client.post(f"/api/estimates/{est['id']}/convert")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "credit_limit"
    r = client.post(
        f"/api/estimates/{est['id']}/convert", json={"allow_over_credit_limit": True}
    )
    assert r.status_code == 200, r.text


def test_the_page_answers_the_servers_question_and_carries_its_own():
    src = open("app/static/js/invoices.js", encoding="utf-8").read()
    helper = src[src.index("async sendAllowingZero(send, flags = {})") :]
    helper = helper[: helper.index("\n    },")]
    assert "code === 'credit_limit' ? 'allow_over_credit_limit'" in helper
    assert "if (!flag || flags[flag]) throw err;" in helper
    # the form's own prompt, once answered, rides along so the server doesn't ask twice
    assert "InvoicesPage._overLimitOk = confirm(" in src
    assert "InvoicesPage._overLimitOk ? { allow_over_credit_limit: true } : {}" in src
    est = open("app/static/js/estimates.js", encoding="utf-8").read()
    assert "InvoicesPage._overLimitOk ? { allow_over_credit_limit: true } : {}" in est
