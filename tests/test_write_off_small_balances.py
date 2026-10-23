"""Write-offs in a business company, and small balances written off from
A/R Aging (#255, 2.22.2).

The write-off to Bad Debt (`POST /api/invoices/{id}/write-off`) worked in
any company, but the invoice list drew its button for a nonprofit only, so
a body shop's $1.75 invoices sitting over 90 days meant a credit memo built
by hand. Now the button is there for every company, and A/R Aging lists the
open balances under a figure and writes them off together, one credit memo
each, refusing a closed period and leaving alone any balance that changed
since it was listed.
"""

from decimal import Decimal

import pytest

from app.models.accounts import Account
from app.models.credit_memos import CreditMemo
from app.models.transactions import Transaction, TransactionLine

D = Decimal


def _invoice(client, customer_id, amount, date="2026-03-01"):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": customer_id,
            "date": date,
            "due_date": date,
            "lines": [{"description": "Service", "quantity": 1, "rate": str(amount)}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _candidates(client, **params):
    r = client.get("/api/invoices/write-offs/candidates", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _gl(db, number):
    acct = db.query(Account).filter(Account.account_number == number).first()
    rows = db.query(TransactionLine).filter_by(account_id=acct.id).all()
    return sum((r.debit for r in rows), D("0")), sum((r.credit for r in rows), D("0"))


def _balanced(db):
    for txn in db.query(Transaction).all():
        dr = sum((ln.debit for ln in txn.lines), D("0"))
        cr = sum((ln.credit for ln in txn.lines), D("0"))
        assert dr == cr, f"transaction {txn.id}: {dr} != {cr}"


@pytest.fixture
def small(client, seed_accounts, seed_customer):
    """Three open invoices in a business company: two under $5, one not."""
    other = client.post("/api/customers", json={"name": "Pier Lumber"}).json()
    return {
        "tiny": _invoice(client, seed_customer.id, "1.75"),
        "small": _invoice(client, other["id"], "4.21"),
        "big": _invoice(client, seed_customer.id, "120.00"),
        "customer": seed_customer.id,
        "other": other["id"],
    }


def test_a_business_company_can_write_off_an_invoice(client, db_session, small):
    r = client.post(
        f"/api/invoices/{small['tiny']['id']}/write-off", json={"date": "2026-10-11"}
    )
    assert r.status_code == 201, r.text
    cm = r.json()
    assert cm["is_write_off"] is True and D(cm["total"]) == D("1.75")
    inv = client.get(f"/api/invoices/{small['tiny']['id']}").json()
    assert inv["status"] == "paid" and D(inv["balance_due"]) == 0
    _balanced(db_session)


def test_the_invoice_list_offers_write_off_to_every_company():
    src = open("app/static/js/invoices.js", encoding="utf-8").read()
    at = src.index("InvoicesPage.showWriteOff(")
    assert "isNonprofit()" not in src[at - 200 : at]
    assert "e.g. too small to collect" in src


def test_candidates_are_the_open_balances_under_the_figure(client, small):
    rows = _candidates(client, under="5", as_of_date="2026-10-11")
    assert sorted((x["id"], x["balance_due"]) for x in rows) == sorted(
        [(small["tiny"]["id"], "1.75"), (small["small"]["id"], "4.21")]
    )
    assert all(
        x["customer_name"] and x["invoice_number"] and x["days_overdue"] > 0
        for x in rows
    )
    # dated after the as-of date, or void: not listed
    assert _candidates(client, under="5", as_of_date="2026-02-01") == []
    client.post(f"/api/invoices/{small['small']['id']}/void")
    assert [x["id"] for x in _candidates(client, under="5")] == [small["tiny"]["id"]]
    r = client.get("/api/invoices/write-offs/candidates", params={"under": "0"})
    assert r.status_code == 422


def test_the_batch_posts_one_credit_memo_each_and_the_books_balance(
    client, db_session, small
):
    ids = [small["tiny"]["id"], small["small"]["id"]]
    r = client.post(
        "/api/invoices/write-offs",
        json={
            "date": "2026-10-11",
            "under": "5",
            "invoice_ids": ids,
            "memo": "too small to collect",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert sorted(w["invoice_id"] for w in body["written_off"]) == sorted(ids)
    assert all(
        w["invoice_number"] and w["memo_number"] and w["credit_memo_id"]
        for w in body["written_off"]
    )
    assert body["skipped"] == []
    memos = db_session.query(CreditMemo).filter(CreditMemo.is_write_off.is_(True)).all()
    assert len(memos) == 2 and all(m.notes == "too small to collect" for m in memos)
    for i in ids:
        inv = client.get(f"/api/invoices/{i}").json()
        assert inv["status"] == "paid" and D(inv["balance_due"]) == 0
    big = client.get(f"/api/invoices/{small['big']['id']}").json()
    assert D(big["balance_due"]) == D("120.00")
    dr, _ = _gl(db_session, "6960")
    assert dr == D("5.96")  # Bad Debt Expense: 1.75 + 4.21
    _balanced(db_session)
    # the aging no longer shows them
    aging = client.get(
        "/api/reports/ar-aging", params={"as_of_date": "2026-10-11"}
    ).json()
    assert D(str(aging["totals"]["total"])) == D("120.00")


def test_a_balance_that_changed_since_it_was_listed_is_left_alone(
    client, db_session, small
):
    listed = _candidates(client, under="5")
    assert sorted(x["id"] for x in listed) == sorted(
        [small["tiny"]["id"], small["small"]["id"]]
    )
    # a payment lands on the second one before the batch posts
    r = client.post(
        "/api/payments",
        json={
            "customer_id": small["other"],
            "date": "2026-10-11",
            "amount": "4.21",
            "method": "check",
            "allocations": [{"invoice_id": small["small"]["id"], "amount": "4.21"}],
        },
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/invoices/write-offs",
        json={
            "date": "2026-10-11",
            "under": "5",
            "invoice_ids": [x["id"] for x in listed] + [999999],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert [w["invoice_id"] for w in body["written_off"]] == [small["tiny"]["id"]]
    assert [(s.get("invoice_id"), s["reason"]) for s in body["skipped"]] == [
        (small["small"]["id"], "already paid"),
        (999999, "not found"),
    ]
    # and one whose balance grew past the figure through an edit is named too
    bigger = _invoice(client, small["customer"], "3.00")
    r = client.put(
        f"/api/invoices/{bigger['id']}",
        json={"lines": [{"description": "Service", "quantity": 1, "rate": "30"}]},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/invoices/write-offs",
        json={"date": "2026-10-11", "under": "5", "invoice_ids": [bigger["id"]]},
    )
    assert r.json()["written_off"] == []
    assert r.json()["skipped"][0]["reason"] == "the balance changed since it was listed"
    _balanced(db_session)


def test_a_closed_period_refuses_the_batch_and_writes_nothing(
    client, db_session, small
):
    r = client.put("/api/settings", json={"closing_date": "2026-10-31"})
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/invoices/write-offs",
        json={"date": "2026-10-11", "under": "5", "invoice_ids": [small["tiny"]["id"]]},
    )
    assert r.status_code == 403, r.text
    assert db_session.query(CreditMemo).count() == 0


def test_the_aging_report_lists_them_before_anything_posts():
    src = open("app/static/js/reports.js", encoding="utf-8").read()
    assert 'id="write-off-under"' in src and "ReportsPage.writeOffSmall(" in src
    assert "API.get(`/invoices/write-offs/candidates?under=" in src
    assert "API.post('/invoices/write-offs'" in src
    assert "write-off-pick" in src and "App.refresh();" in src


def test_write_off_is_on_the_invoices_view_as_well_as_its_row():
    """Gate round 1, W-2: the row had Write Off, the view did not."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app/static/js/invoices.js").read_text(
        encoding="utf-8"
    )
    assert (
        src.count(
            'onclick="InvoicesPage.showWriteOff(${inv.id}, ${parseFloat(inv.balance_due)})">Write Off</button>'
        )
        == 2
    )
