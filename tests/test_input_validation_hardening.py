"""Hostile-scalar hardening, through real endpoints.

A TestClient audit found four classes of malformed input that passed schema
validation and blew up downstream as an uncaught 500:

  1. A non-finite float (NaN / Infinity) in any float field — a bill line
     quantity/rate hit decimal.InvalidOperation, a bill-payment amount
     became a NULL insert (IntegrityError).
  2. A NUL byte (\\x00) in any string — stored silently on SQLite, a 500
     on PostgreSQL.
  3. A document date at the edges of the date type — an invoice dated
     9999-12-31 overflowed computing its Net-30 due date.
  4. A PUT that explicitly sets a NOT NULL column to null — IntegrityError.

The fix is central (app/schemas/common.StrictModel and a routes helper), so
these pin a representative field for each class to a 422, not a 500.
"""

import json

# JSON has no NaN/Infinity literal, so httpx refuses to serialize one. A real
# client sends them as the non-standard bare tokens Python's json module emits
# (and accepts) — post the raw body the way a hostile caller would.
_JSON = {"Content-Type": "application/json"}


def _raw(client, method, url, payload):
    body = json.dumps(payload)  # allow_nan=True: writes NaN / Infinity
    return client.request(method, url, content=body, headers=_JSON)


def _vendor(db_session, name="V"):
    from app.models.accounts import Account
    from app.models.contacts import Vendor

    expense = db_session.query(Account).filter_by(account_number="6000").first()
    v = Vendor(
        name=name,
        is_active=True,
        default_expense_account_id=expense.id if expense else None,
    )
    db_session.add(v)
    db_session.commit()
    return v


# ---------------------------------------------------------------------------
# 1. Non-finite floats
# ---------------------------------------------------------------------------


def test_bill_line_quantity_nan_is_422(client, db_session, seed_accounts):
    vendor = _vendor(db_session)
    r = _raw(
        client,
        "POST",
        "/api/bills",
        {
            "vendor_id": vendor.id,
            "bill_number": "B-NAN",
            "date": "2026-05-01",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": float("nan"), "rate": 1}],
        },
    )
    assert r.status_code == 422, r.text


def test_bill_line_rate_infinity_is_422(client, db_session, seed_accounts):
    vendor = _vendor(db_session)
    r = _raw(
        client,
        "POST",
        "/api/bills",
        {
            "vendor_id": vendor.id,
            "bill_number": "B-INF",
            "date": "2026-05-01",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": float("inf")}],
        },
    )
    assert r.status_code == 422, r.text


def test_bill_payment_amount_nan_is_422(client, db_session, seed_accounts):
    vendor = _vendor(db_session)
    r = _raw(
        client,
        "POST",
        "/api/bill-payments",
        {"vendor_id": vendor.id, "date": "2026-05-01", "amount": float("nan")},
    )
    assert r.status_code == 422, r.text


def test_credit_memo_line_rate_nan_is_422(
    client, db_session, seed_accounts, seed_customer
):
    r = _raw(
        client,
        "POST",
        "/api/credit-memos",
        {
            "customer_id": seed_customer.id,
            "date": "2026-05-01",
            "lines": [{"description": "x", "quantity": 1, "rate": float("nan")}],
        },
    )
    assert r.status_code == 422, r.text


def test_benefit_rate_nan_is_422(client, db_session, seed_accounts):
    r = _raw(
        client,
        "POST",
        "/api/benefits/codes",
        {
            "code": "DENTAL",
            "name": "Dental",
            "rate": {"employee_rate": float("nan"), "employer_rate": 0},
        },
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 2. NUL byte in strings
# ---------------------------------------------------------------------------


def test_customer_name_with_nul_byte_is_422(client, db_session):
    r = client.post("/api/customers", json={"name": "Nul\x00Co"})
    assert r.status_code == 422, r.text


def test_bill_notes_with_nul_byte_is_422(client, db_session, seed_accounts):
    vendor = _vendor(db_session)
    r = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor.id,
            "bill_number": "B-NUL",
            "date": "2026-05-01",
            "tax_rate": 0,
            "notes": "a\x00b",
            "lines": [{"description": "x", "quantity": 1, "rate": 1}],
        },
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 3. Out-of-range dates
# ---------------------------------------------------------------------------


def test_invoice_far_future_date_is_422_not_500(
    client, db_session, seed_accounts, seed_customer
):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "9999-12-31",
            "terms": "Net 30",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": 1}],
        },
    )
    assert r.status_code == 422, r.text


def test_invoice_year_one_date_is_422(client, db_session, seed_accounts, seed_customer):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "0001-01-01",
            "terms": "Net 30",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": 1}],
        },
    )
    assert r.status_code == 422, r.text


def test_invoice_ordinary_date_still_accepted(
    client, db_session, seed_accounts, seed_customer
):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-05-01",
            "terms": "Net 30",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": 1}],
        },
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# 4. Explicit null on a NOT NULL column
# ---------------------------------------------------------------------------


def test_put_customer_name_null_is_422(client, db_session, seed_customer):
    r = client.put(f"/api/customers/{seed_customer.id}", json={"name": None})
    assert r.status_code == 422, r.text
    # The non-null name survived.
    assert (
        client.get(f"/api/customers/{seed_customer.id}").json()["name"]
        == "Test Customer"
    )


def test_put_vendor_name_null_is_422(client, db_session):
    vendor = _vendor(db_session, name="Acme")
    r = client.put(f"/api/vendors/{vendor.id}", json={"name": None})
    assert r.status_code == 422, r.text


def test_put_customer_empty_body_stays_noop_200(client, db_session, seed_customer):
    r = client.put(f"/api/customers/{seed_customer.id}", json={})
    assert r.status_code == 200, r.text


def _posted_bill(client, db_session, seed_accounts, number="B-100"):
    vendor = _vendor(db_session)
    r = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor.id,
            "bill_number": number,
            "date": "2026-05-01",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": 10}],
        },
    )
    assert r.status_code == 201, r.text
    return vendor, r.json()


def test_put_bill_cleared_number_generates_one(client, db_session, seed_accounts):
    """The desktop's edit form sends a cleared bill number as null (the
    exact shape bills.js builds). That is not a null on a NOT NULL column
    to refuse: the route generates a number, as main did."""
    vendor, bill = _posted_bill(client, db_session, seed_accounts)
    body = {
        "vendor_id": vendor.id,
        "bill_number": None,
        "date": "2026-05-01",
        "terms": "Net 30",
        "notes": None,
        "class_id": None,
        "job_id": None,
        "lines": [{"description": "x", "quantity": 1, "rate": 10}],
    }
    r = client.put(f"/api/bills/{bill['id']}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["bill_number"]
    assert r.json()["bill_number"] != "B-100"


def test_put_bill_vendor_id_null_is_still_422(client, db_session, seed_accounts):
    _, bill = _posted_bill(client, db_session, seed_accounts)
    r = client.put(f"/api/bills/{bill['id']}", json={"vendor_id": None})
    assert r.status_code == 422, r.text
    assert "vendor_id" in r.json()["detail"]
    r = client.put(f"/api/bills/{bill['id']}", json={"date": None})
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 5. NUL inside a list[str]; BudgetUpdate is a StrictModel
# ---------------------------------------------------------------------------


def test_nul_byte_inside_list_of_strings_is_rejected():
    from pydantic import ValidationError
    import pytest

    from app.routes.simplefin import MapRequest

    MapRequest(mapping={}, reversed=["acct-1"])
    with pytest.raises(ValidationError, match="NUL"):
        MapRequest(mapping={}, reversed=["acct-1", "bad\x00id"])


def test_budget_update_refuses_unknown_fields():
    from decimal import Decimal

    from pydantic import ValidationError
    import pytest

    from app.schemas.budgets import BudgetUpdate
    from app.schemas.common import StrictModel

    assert issubclass(BudgetUpdate, StrictModel)
    assert BudgetUpdate(amount=Decimal("5")).amount == Decimal("5")
    with pytest.raises(ValidationError):
        BudgetUpdate(amount=Decimal("5"), ammount=Decimal("1"))
