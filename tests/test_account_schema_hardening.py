"""Account schema hardening: NULL balances and blank account numbers.

- accounts.balance is nullable in practice (legacy/imported rows); the
  response schema must coalesce NULL to 0 instead of 500ing on read.
- accounts.account_number is unique; a blank string must normalize to
  NULL or the second no-number account collides with the first. Since the
  2.17.3 exploratory test (W-L4) the form and the API refuse a blank or
  non-numeric number outright, with a sentence rather than a 500; an
  account an importer left without a number can still be renamed.
"""

from sqlalchemy import text

from app.models.accounts import Account, AccountType


def test_null_balance_account_reads_as_zero(client, db_session):
    account = Account(name="Legacy NULL balance", account_type=AccountType.EXPENSE)
    db_session.add(account)
    db_session.commit()
    db_session.execute(
        text("UPDATE accounts SET balance = NULL WHERE id = :id"),
        {"id": account.id},
    )
    db_session.commit()

    resp = client.get(f"/api/accounts/{account.id}")
    assert resp.status_code == 200
    assert resp.json()["balance"] == "0"


def test_blank_account_number_normalizes_to_null():
    from app.schemas.accounts import AccountCreate, AccountUpdate

    # the schema still reads a blank as "no number"...
    assert (
        AccountCreate(name="x", account_type="expense", account_number="  ")
    ).account_number is None
    assert AccountUpdate(account_number="").account_number is None


def test_a_new_account_without_a_number_saves_with_none(client):
    # A number is optional (#332, 2.23.5): a blank is stored as NULL, which
    # the unique constraint allows any number of, and two such accounts
    # live side by side.
    ids = []
    for blank in ("", "   ", None):
        r = client.post(
            "/api/accounts",
            json={
                "name": f"No number {len(ids)}",
                "account_type": "expense",
                "account_number": blank,
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["account_number"] is None
        ids.append(r.json()["id"])
    assert len(set(ids)) == 3
    bad = client.post(
        "/api/accounts",
        json={"name": "Bad", "account_type": "expense", "account_number": "7x7"},
    )
    assert bad.status_code == 400 and "not an account number" in bad.json()["detail"]


def test_removing_a_number_on_update_clears_it(client):
    created = client.post(
        "/api/accounts",
        json={
            "name": "Numbered",
            "account_type": "expense",
            "account_number": "9998",
        },
    )
    assert created.status_code == 201
    account_id = created.json()["id"]

    updated = client.put(f"/api/accounts/{account_id}", json={"account_number": ""})
    assert updated.status_code == 200, updated.text
    assert client.get(f"/api/accounts/{account_id}").json()["account_number"] is None


def test_an_imported_account_without_a_number_can_still_be_renamed(client, db_session):
    account = Account(name="From hledger", account_type=AccountType.EXPENSE)
    db_session.add(account)
    db_session.commit()
    r = client.put(
        f"/api/accounts/{account.id}",
        json={"name": "Renamed", "account_number": "", "account_type": "expense"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed" and r.json()["account_number"] is None
