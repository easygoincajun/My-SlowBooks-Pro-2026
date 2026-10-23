"""Clear a whole statement at once (#258, 2.22.2).

Each line on the reconcile screen was one checkbox posting one toggle. A
quiet month, or a first reconciliation where the statement matches the
books, was hundreds of single ticks. Now Mark all, Mark through a date and
Clear all tick or untick every line the session shows, lines after the
statement date never among them, and answer with the new difference.
"""

import pytest


@pytest.fixture
def vendor(client):
    return client.post("/api/vendors", json={"name": "Grid Power"}).json()


def _expense(client, seed_accounts, vendor, amount, day):
    r = client.post(
        "/api/expenses",
        json={
            "date": f"2026-09-{day:02d}",
            "vendor_id": vendor["id"],
            "expense_account_id": seed_accounts["6000"].id,
            "paid_from_account_id": seed_accounts["1000"].id,
            "amount": amount,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _deposit(client, seed_accounts, amount, day):
    r = client.post(
        "/api/banking/transactions",
        json={
            "account_id": seed_accounts["1000"].id,
            "date": f"2026-09-{day:02d}",
            "amount": amount,
            "category_account_id": seed_accounts["4000"].id,
            "payee": "walk-in",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _start(client, seed_accounts, balance, day):
    r = client.post(
        "/api/banking/reconciliations",
        json={
            "account_id": seed_accounts["1000"].id,
            "statement_date": f"2026-09-{day:02d}",
            "statement_balance": balance,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _session(client, recon_id):
    return client.get(f"/api/banking/reconciliations/{recon_id}/transactions").json()


def _mark(client, recon_id, **body):
    return client.post(f"/api/banking/reconciliations/{recon_id}/mark-all", json=body)


@pytest.fixture
def statement(client, seed_accounts, vendor):
    """Four lines in September, a statement dated the 20th: the 30th's line
    is not on the session."""
    _deposit(client, seed_accounts, "1000", 1)
    _expense(client, seed_accounts, vendor, "150", 5)
    _expense(client, seed_accounts, vendor, "40", 12)
    _deposit(client, seed_accounts, "999", 30)
    return _start(client, seed_accounts, "810.00", day=20)


def test_mark_all_ticks_every_line_on_the_session_and_balances(client, statement):
    r = _mark(client, statement["id"], cleared=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == 3
    assert body["difference"] == 0.0 and body["cleared_total"] == 810.0
    assert all(t["reconciled"] for t in body["transactions"])
    assert len(body["transactions"]) == 3  # the 30th is after the statement
    # again: nothing to change
    assert _mark(client, statement["id"], cleared=True).json()["changed"] == 0
    # Finish works
    r = client.post(f"/api/banking/reconciliations/{statement['id']}/complete")
    assert r.status_code == 200, r.text


def test_mark_through_a_date_ticks_only_up_to_it(client, statement):
    r = _mark(client, statement["id"], cleared=True, through="2026-09-05")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == 2
    by_date = {t["date"]: t["reconciled"] for t in body["transactions"]}
    assert by_date == {"2026-09-01": True, "2026-09-05": True, "2026-09-12": False}
    assert body["cleared_total"] == 850.0 and body["difference"] == -40.0
    # past the statement date: refused
    r = _mark(client, statement["id"], cleared=True, through="2026-09-25")
    assert r.status_code == 400 and "after the statement date" in r.json()["detail"]


def test_clear_all_unticks_and_a_single_tick_still_works(client, statement):
    _mark(client, statement["id"], cleared=True)
    r = _mark(client, statement["id"], cleared=False)
    assert r.json()["changed"] == 3 and r.json()["cleared_total"] == 0.0
    s = _session(client, statement["id"])
    assert not any(t["reconciled"] for t in s["transactions"])
    line = s["transactions"][0]["id"]
    r = client.post(f"/api/banking/reconciliations/{statement['id']}/toggle/{line}")
    assert r.status_code == 200 and r.json()["reconciled"] is True


def test_a_completed_reconciliation_refuses_it(client, statement):
    _mark(client, statement["id"], cleared=True)
    client.post(f"/api/banking/reconciliations/{statement['id']}/complete")
    r = _mark(client, statement["id"], cleared=False)
    assert r.status_code == 400 and "already completed" in r.json()["detail"]


def test_the_reconcile_screen_has_the_three_buttons():
    src = open("app/static/js/banking.js", encoding="utf-8").read()
    assert "BankingPage.markAll(${reconId}, true)" in src
    assert "BankingPage.markAll(${reconId}, false)" in src
    assert 'id="recon-through"' in src and 'max="${data.statement_date}"' in src
    assert "API.post(`/banking/reconciliations/${reconId}/mark-all`" in src
