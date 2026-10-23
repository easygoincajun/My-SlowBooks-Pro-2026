"""A categorized bank sheet as posted transactions (#328): the Transaction
Pro / SaasAnt layout a bookkeeper fills in and bulk-posts. The fixture is
an invented sheet in the exact layout of the template a bookkeeper sent
(the bank account named in the header, the Amount header padded); the
real template stays private."""

import io
from pathlib import Path

from app.models.contacts import Customer, Vendor
from app.models.transactions import Transaction

ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / "tests/fixtures/qb_desktop/Bank_Acct_Template.csv"


def _upload(client, text=None, name="Bank Acct Template.csv", **fields):
    raw = (text or SHEET.read_text(encoding="utf-8")).encode("utf-8")
    data = {k: ("true" if v else "false") for k, v in fields.items()}
    return client.post(
        "/api/csv/import/qb-report",
        files={"file": (name, io.BytesIO(raw), "text/csv")},
        data=data,
    )


def _register(client, account_id):
    return client.get(f"/api/banking/check-register?account_id={account_id}").json()[
        "entries"
    ]


def test_the_sheet_posts_each_row_as_the_entry_it_is(client, db_session, seed_accounts):
    r = _upload(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected"] == "categorized", body
    assert body["errors"] == [], body["errors"]
    # seven rows: five entries, one of them a split of two rows, one a transfer
    assert body["posted"] == 6 and body["splits"] == 1
    assert body["report"].startswith("Categorized transactions")
    reg = _register(client, seed_accounts["1000"].id)
    assert len(reg) == 6
    by_payee = {(e["date"], e.get("payee") or ""): e for e in reg}
    assert by_payee[("2026-03-02", "Harbor Insurance")]["payment"] == 220.0
    assert by_payee[("2026-03-05", "Walk-in")]["deposit"] == 1250.0
    assert by_payee[("2026-03-15", "Walk-in")]["deposit"] == 300.0
    # the two Harbor Bank rows are one entry of three lines
    loan = by_payee[("2026-03-09", "Harbor Bank")]
    assert loan["payment"] == 500.0
    entry = client.get(f"/api/journal/{loan['transaction_id']}").json()
    assert len(entry["lines"]) == 3
    assert {
        (ln["account_number"], ln["debit"]) for ln in entry["lines"] if ln["debit"]
    } == {("6300", 120.0), ("6000", 380.0)}
    # the row to Savings is a transfer, on both registers
    move = next(e for e in reg if e["date"] == "2026-03-12")
    assert move["payment"] == 400.0 and move["source_type"] == "transfer"
    assert any(
        e["deposit"] == 400.0 for e in _register(client, seed_accounts["1010"].id)
    )
    # payees not on the lists were added: vendors for money out, customers for money in
    vendors = {v.name for v in db_session.query(Vendor).all()}
    customers = {c.name for c in db_session.query(Customer).all()}
    assert {"Harbor Insurance", "Print Shop", "Harbor Bank"} <= vendors
    assert "Walk-in" in customers and "Walk-in" not in vendors
    assert any("were added from their names" in w for w in body["warnings"])
    # a second upload posts nothing twice
    again = _upload(client).json()
    assert again["posted"] == 0 and again["splits"] == 0
    assert again["duplicates_skipped"] == 6
    assert len(_register(client, seed_accounts["1000"].id)) == 6


def test_the_file_is_refused_whole_when_an_account_is_missing(
    client, db_session, seed_accounts
):
    text = SHEET.read_text(encoding="utf-8").replace(
        "Advertising & Marketing", "Nowhere Fund"
    )
    body = _upload(client, text).json()
    assert body["posted"] == 0
    assert any(
        "Nowhere Fund" in e and "Nothing was posted" in e for e in body["errors"]
    ), body["errors"]
    assert db_session.query(Transaction).count() == 0
    bad_bank = SHEET.read_text(encoding="utf-8").replace(
        "Bank Account Checking", "Bank Account Elsewhere 1234"
    )
    body = _upload(client, bad_bank).json()
    assert body["posted"] == 0 and any("Elsewhere 1234" in e for e in body["errors"])
    unreadable = SHEET.read_text(encoding="utf-8").replace("03/04/2026", "yesterday")
    body = _upload(client, unreadable).json()
    assert body["posted"] == 0 and any("could not be read" in e for e in body["errors"])
    assert db_session.query(Transaction).count() == 0


def test_the_bank_account_can_be_named_per_row_or_by_its_last_digits(
    client, db_session, seed_accounts
):
    text = (
        "Bank Account,Transaction Date,Memo,Payee,Account,Amount\n"
        "Checking,2026-04-01,Rent,Harbor Properties,6300,-1000\n"
        "Savings,2026-04-02,Interest,,4000,12.34\n"
    )
    body = _upload(client, text).json()
    assert body["errors"] == [] and body["posted"] == 2
    assert _register(client, seed_accounts["1010"].id)[0]["deposit"] == 12.34
    # the template's way: the bank in the header, by the digits a statement shows
    acct = seed_accounts["1000"]
    acct.name = "Bank of America ...9959"
    db_session.commit()
    text = "Bank Account BofA 9959,Transaction Date,Memo,Payee,Account,Amount\n,2026-04-03,Stamps,Post Office,6300,-12\n"
    body = _upload(client, text).json()
    assert body["errors"] == [] and body["posted"] == 1
    assert any(e["payment"] == 12.0 for e in _register(client, acct.id))


def test_a_sheet_with_money_out_positive_is_read_the_other_way_round(
    client, db_session, seed_accounts
):
    text = "Bank Account Checking,Transaction Date,Memo,Payee,Account,Amount\n,2026-05-01,Stamps,Post Office,6300,12.00\n"
    body = _upload(client, text, outflow_positive=True).json()
    assert body["errors"] == [] and body["posted"] == 1
    entry = _register(client, seed_accounts["1000"].id)[0]
    assert entry["payment"] == 12.0 and entry["deposit"] == 0


def test_the_page_offers_the_sheet_and_its_sign_box():
    js = (ROOT / "app/static/js/iif.js").read_text(encoding="utf-8")
    assert "A categorized bank sheet" in js
    assert (
        'id="qbcsv-outflow-positive"' in js
        and "formData.append('outflow_positive'," in js
    )
    assert "categorized: 'Categorized bank sheet'" in js
