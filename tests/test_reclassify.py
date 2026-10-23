"""Reclassify (#337): the lines on an account, each saying whether it can
move and why not; a dry run that changes nothing; the move itself, as one
audited change naming every line, the bill's own line following; the
closing date respected; the accountant role allowed, read-only not."""

from urllib.parse import quote

from app.models.bills import BillLine
from app.models.transactions import TransactionLine
from app.models.users import ROLE_ACCOUNTANT, ROLE_READONLY, User
from app.services import auth as auth_service

CPA_PW = "ledger-and-lamplight"
OVERRIDE_PW = "period-is-closed"


def _ok(r, code=(200, 201)):
    assert r.status_code in code, r.text
    return r.json()


def _journal(client, accounts, debit_no, credit_no, amount, text, day="2026-03-10"):
    return _ok(
        client.post(
            "/api/journal",
            json={
                "date": day,
                "description": text,
                "lines": [
                    {
                        "account_id": accounts[debit_no].id,
                        "debit": amount,
                        "credit": "0",
                    },
                    {
                        "account_id": accounts[credit_no].id,
                        "debit": "0",
                        "credit": amount,
                    },
                ],
            },
        )
    )


def _bank_entry(client, accounts, category_no, amount, text, day="2026-03-12"):
    return _ok(
        client.post(
            "/api/banking/transactions",
            json={
                "account_id": accounts["1000"].id,
                "date": day,
                "amount": amount,
                "category_account_id": accounts[category_no].id,
                "payee": "Corner Hardware",
                "description": text,
            },
        )
    )


def _bill(client, accounts, account_no, amount, number="B-1", day="2026-03-14"):
    vendor = _ok(client.post("/api/vendors", json={"name": f"Vendor {number}"}))
    return _ok(
        client.post(
            "/api/bills",
            json={
                "vendor_id": vendor["id"],
                "bill_number": number,
                "date": day,
                "lines": [
                    {
                        "description": "Parked",
                        "quantity": 1,
                        "rate": amount,
                        "account_id": accounts[account_no].id,
                    }
                ],
            },
        )
    )


def _lines(client, accounts, account_no, start="2026-01-01", end="2026-12-31"):
    return _ok(
        client.get(
            f"/api/reclassify/lines?account_id={accounts[account_no].id}"
            f"&start_date={start}&end_date={end}"
        )
    )


def _line_ids(client, db, account_no, accounts):
    return [
        ln.id
        for ln in db.query(TransactionLine)
        .filter(TransactionLine.account_id == accounts[account_no].id)
        .order_by(TransactionLine.id)
        .all()
    ]


def test_the_list_says_which_lines_can_move_and_why_not(
    client, db_session, seed_accounts, seed_customer
):
    accounts = seed_accounts
    # parked in Other Expenses (6900): a journal, a bank entry, a bill line
    _journal(client, accounts, "6900", "2000", "40.00", "Unknown charge")
    _bank_entry(client, accounts, "6900", "-75.00", "Card swipe, unknown")
    _bill(client, accounts, "6900", 120)
    # an invoice posts to income through its item: that line stays put
    _ok(
        client.post(
            "/api/invoices",
            json={
                "customer_id": seed_customer.id,
                "date": "2026-03-15",
                "lines": [{"description": "Service", "quantity": 1, "rate": 100}],
            },
        )
    )
    parked = _lines(client, accounts, "6900")
    assert parked["count"] == 3 and parked["movable"] == 3
    assert parked["movable_total"] == "235.00"
    labels = sorted(ln["source_label"] for ln in parked["lines"])
    assert labels == ["Bank entry", "Bill", "Journal entry"]
    bill_line = next(ln for ln in parked["lines"] if ln["source_label"] == "Bill")
    assert bill_line["document"] == "Bill B-1"

    # the bank side of the register entry stays, and says so
    bank = _lines(client, accounts, "1000")
    assert bank["count"] == 1 and bank["movable"] == 0
    assert "bank side" in bank["lines"][0]["reason"]
    # the invoice's income line comes from the document
    income = _lines(client, accounts, "4000")
    assert income["movable"] == 0
    assert "comes from the document" in income["lines"][0]["reason"]
    # Accounts Payable is the documents' side
    ap = _lines(client, accounts, "2000")
    assert ap["movable"] == 0
    assert all("Accounts Payable" in ln["reason"] for ln in ap["lines"])


def test_a_dry_run_plans_and_changes_nothing(client, db_session, seed_accounts):
    accounts = seed_accounts
    _journal(client, accounts, "6900", "2000", "40.00", "Unknown charge")
    ids = _line_ids(client, db_session, "6900", accounts)
    plan = _ok(
        client.post(
            "/api/reclassify",
            json={"line_ids": ids, "account_id": accounts["6300"].id},
        )
    )
    assert plan["dry_run"] is True and plan["count"] == 1
    assert plan["moved"][0]["from_account"]["name"] == accounts["6900"].name
    assert plan["moved"][0]["to_account"]["name"] == accounts["6300"].name
    db_session.expire_all()
    assert _line_ids(client, db_session, "6300", accounts) == []
    assert _line_ids(client, db_session, "6900", accounts) == ids


def test_the_move_is_one_audited_change_and_the_bill_follows(
    client, db_session, seed_accounts
):
    accounts = seed_accounts
    _journal(client, accounts, "6900", "2000", "40.00", "Unknown charge")
    _bank_entry(client, accounts, "6900", "-75.00", "Card swipe, unknown")
    bill = _bill(client, accounts, "6900", 120)
    klass = _ok(client.post("/api/classes", json={"name": "Shop"}))
    ids = _line_ids(client, db_session, "6900", accounts)
    assert len(ids) == 3

    done = _ok(
        client.post(
            "/api/reclassify",
            json={
                "line_ids": ids,
                "account_id": accounts["6300"].id,
                "class_id": klass["id"],
                "dry_run": False,
            },
        )
    )
    assert done["dry_run"] is False and done["count"] == 3 and done["skipped"] == []
    assert {m["line_id"] for m in done["moved"]} == set(ids)
    assert all(m["to_class"]["name"] == "Shop" for m in done["moved"])
    followed = [m for m in done["moved"] if m["document"] == "Bill B-1"]
    assert followed and followed[0]["document_line_follows"] is True

    db_session.expire_all()
    assert _line_ids(client, db_session, "6900", accounts) == []
    moved = db_session.query(TransactionLine).filter(TransactionLine.id.in_(ids)).all()
    assert all(ln.account_id == accounts["6300"].id for ln in moved)
    assert all(ln.class_id == klass["id"] for ln in moved)
    # the bill's own line agrees with its posting
    bl = db_session.query(BillLine).filter(BillLine.bill_id == bill["id"]).one()
    assert bl.account_id == accounts["6300"].id and bl.class_id == klass["id"]
    # the bill's page shows the new account too
    shown = _ok(client.get(f"/api/bills/{bill['id']}"))
    assert shown["lines"][0]["account_id"] == accounts["6300"].id

    # one event for the move, every line named, beside each line's own
    events = _ok(client.get("/api/audit?table_name=reclassify"))
    rows = events["entries"] if isinstance(events, dict) else events
    assert len(rows) == 1
    event = rows[0]
    assert event["source"] == "reclassify"
    assert sorted(event["new_values"]["lines"]) == sorted(ids)
    assert event["new_values"]["account"]["name"] == accounts["6300"].name
    per_line = _ok(client.get("/api/audit?table_name=transaction_lines"))
    per_line = per_line["entries"] if isinstance(per_line, dict) else per_line
    changed = [
        e
        for e in per_line
        if e["action"] == "UPDATE" and "account_id" in (e["changed_fields"] or [])
    ]
    assert {e["record_id"] for e in changed} >= set(ids)

    # the moved lines now list on the account they went to, with the class
    there = _lines(client, accounts, "6300")
    assert there["count"] == 3
    assert all(ln["class_name"] == "Shop" for ln in there["lines"])
    # the list on the old account is empty
    assert _lines(client, accounts, "6900")["count"] == 0


def test_a_line_already_there_and_a_bad_target_are_refused(
    client, db_session, seed_accounts
):
    accounts = seed_accounts
    _journal(client, accounts, "6900", "2000", "40.00", "Unknown charge")
    ids = _line_ids(client, db_session, "6900", accounts)
    # the same account: nothing to do
    r = _ok(
        client.post(
            "/api/reclassify",
            json={"line_ids": ids, "account_id": accounts["6900"].id, "dry_run": False},
        )
    )
    assert r["count"] == 0 and r["skipped"][0]["reason"] == "already there"
    # a bank account is not a target
    r = client.post(
        "/api/reclassify",
        json={"line_ids": ids, "account_id": accounts["1000"].id, "dry_run": False},
    )
    assert r.status_code == 400 and "register" in r.json()["detail"]
    # nor Accounts Payable
    r = client.post(
        "/api/reclassify",
        json={"line_ids": ids, "account_id": accounts["2000"].id, "dry_run": False},
    )
    assert r.status_code == 400 and "Accounts Payable" in r.json()["detail"]
    # neither an account nor a class: nothing asked
    r = client.post("/api/reclassify", json={"line_ids": ids, "dry_run": False})
    assert r.status_code == 400
    # the bank side cannot be sent
    bank_ids = _line_ids(client, db_session, "2000", accounts)
    r = _ok(
        client.post(
            "/api/reclassify",
            json={
                "line_ids": bank_ids,
                "account_id": accounts["6300"].id,
                "dry_run": False,
            },
        )
    )
    assert r["count"] == 0 and "Accounts Payable" in r["skipped"][0]["reason"]


def test_a_voided_entrys_lines_stay(client, db_session, seed_accounts):
    accounts = seed_accounts
    entry = _journal(client, accounts, "6900", "2000", "40.00", "Unknown charge")
    _ok(client.post(f"/api/journal/{entry['id']}/void"))
    listed = _lines(client, accounts, "6900")
    assert listed["count"] == 2 and listed["movable"] == 0
    assert all(ln["reason"] == "voided" for ln in listed["lines"])


def test_the_closing_date_holds_and_the_password_opens_it(
    client, db_session, seed_accounts
):
    accounts = seed_accounts
    _journal(client, accounts, "6900", "2000", "40.00", "Old charge", day="2026-01-20")
    _ok(
        client.put(
            "/api/settings",
            json={"closing_date": "2026-02-28", "closing_date_password": OVERRIDE_PW},
        )
    )
    ids = _line_ids(client, db_session, "6900", accounts)
    body = {"line_ids": ids, "account_id": accounts["6300"].id, "dry_run": False}
    r = client.post("/api/reclassify", json=body)
    assert r.status_code == 403, r.text
    db_session.expire_all()
    assert _line_ids(client, db_session, "6900", accounts) == ids
    r = client.post(
        "/api/reclassify",
        json=body,
        headers={"X-Closing-Date-Password": quote(OVERRIDE_PW)},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert _line_ids(client, db_session, "6300", accounts) == ids
    # the Closing Date Exceptions report lists the lines the move changed
    report = _ok(
        client.get(
            "/api/reports/closing-date-exceptions?start_date=2026-01-01&end_date=2026-12-31"
        )
    )
    assert report["count"] == 1
    records = [c["record"] for c in report["events"][0]["changes"]]
    assert "journal line" in records


def _mk_user(db, username, password, role):
    u = User(
        username=username,
        display_name=username.title(),
        password_hash=auth_service.hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(u)
    db.commit()
    return u


def _login_as(client, username, password):
    client.post("/api/auth/logout")
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text


def test_the_accountant_may_move_lines_and_read_only_may_not(
    client, db_session, seed_accounts
):
    accounts = seed_accounts
    _journal(client, accounts, "6900", "2000", "40.00", "Unknown charge")
    ids = _line_ids(client, db_session, "6900", accounts)
    _mk_user(db_session, "cpa", CPA_PW, ROLE_ACCOUNTANT)
    _mk_user(db_session, "viewer", CPA_PW, ROLE_READONLY)

    _login_as(client, "viewer", CPA_PW)
    assert _lines(client, accounts, "6900")["count"] == 1  # the list is a read
    r = client.post(
        "/api/reclassify",
        json={"line_ids": ids, "account_id": accounts["6300"].id, "dry_run": False},
    )
    assert r.status_code == 403

    _login_as(client, "cpa", CPA_PW)
    r = _ok(
        client.post(
            "/api/reclassify",
            json={"line_ids": ids, "account_id": accounts["6300"].id, "dry_run": False},
        )
    )
    assert r["count"] == 1
    db_session.expire_all()
    assert _line_ids(client, db_session, "6300", accounts) == ids
    # the move is in the accountant's name
    events = _ok(client.get("/api/audit?table_name=reclassify"))
    rows = events["entries"] if isinstance(events, dict) else events
    assert rows[0]["username"] == "cpa"


def test_the_report_center_offers_the_tool_and_the_page_keeps_it_open_to_the_accountant():
    from pathlib import Path

    src = Path("app/static/js/reports.js").read_text(encoding="utf-8")
    assert "card('reclassify'" in src
    assert "'reclassify':" in src and "ReportsPage.reclassify(p)" in src
    # the move controls stay usable for the accountant and hidden from
    # read-only: the container carries data-accountant-ok, the buttons data-write
    assert 'id="rc-move" data-accountant-ok' in src
    assert 'data-write onclick="ReportsPage.rcPreview()"' in src
    assert 'data-write onclick="ReportsPage.rcMove()"' in src
