"""Closing Date Exceptions (#342): a change made into a closed period with
the closing-date password appears, with the rows it touched before and
after; ordinary posting after the closing date does not; the report saves
as PDF and CSV."""

from pathlib import Path
from urllib.parse import quote

from tests.conftest import WEASYPRINT_AVAILABLE

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_PW = "close-the-books-26"
PERIOD = "start_date=2020-01-01&end_date=2099-12-31"


def _customer(client):
    r = client.post("/api/customers", json={"name": "Salt & Pine Catering"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _invoice(customer_id, day):
    return {
        "customer_id": customer_id,
        "date": day,
        "lines": [{"description": "Cake", "quantity": 1, "rate": 450}],
    }


def _close(client, day="2026-06-30"):
    r = client.put(
        "/api/settings",
        json={"closing_date": day, "closing_date_password": OVERRIDE_PW},
    )
    assert r.status_code == 200, r.text


def _journal(client, accounts, day, amount="25.00", **headers):
    return client.post(
        "/api/journal",
        json={
            "date": day,
            "description": "Entry",
            "lines": [
                {"account_id": accounts["6300"].id, "debit": amount, "credit": "0"},
                {"account_id": accounts["1000"].id, "debit": "0", "credit": amount},
            ],
        },
        **headers,
    )


def _report(client):
    r = client.get(f"/api/reports/closing-date-exceptions?{PERIOD}")
    assert r.status_code == 200, r.text
    return r.json()


def test_a_change_with_the_password_appears_and_ordinary_posting_does_not(
    client, seed_accounts
):
    cid = _customer(client)
    # an invoice in the period, before it is closed
    inv = client.post("/api/invoices", json=_invoice(cid, "2026-03-01"))
    assert inv.status_code == 201, inv.text
    _close(client)
    assert _report(client)["count"] == 0

    # a change into the closed period, with the password
    r = client.put(
        f"/api/invoices/{inv.json()['id']}",
        json={
            **_invoice(cid, "2026-03-02"),
            "lines": [{"description": "Cake", "quantity": 1, "rate": 475}],
        },
        headers={"X-Closing-Date-Password": quote(OVERRIDE_PW)},
    )
    assert r.status_code == 200, r.text
    # and a posting after the closing date, no password needed
    assert _journal(client, seed_accounts, "2026-09-01").status_code == 201
    # and one refused inside it, which is no exception either
    assert _journal(client, seed_accounts, "2026-05-01").status_code == 403

    data = _report(client)
    assert data["closing_date"] == "2026-06-30"
    assert data["count"] == 1
    ev = data["events"][0]
    assert ev["who"] == "admin"
    # the date the check met first: the invoice's, before the change
    assert ev["closing_date"] == "2026-06-30" and ev["transaction_date"] == "2026-03-01"
    changed = {(c["table"], c["action"]) for c in ev["changes"]}
    assert ("invoices", "update") in changed
    inv_change = next(c for c in ev["changes"] if c["table"] == "invoices")
    assert (
        inv_change["record"] == "invoice"
        and inv_change["record_id"] == inv.json()["id"]
    )
    fields = {f["field"]: (f["before"], f["after"]) for f in inv_change["fields"]}
    assert fields["date"] == ("2026-03-01", "2026-03-02")
    # the journal entry posted after the closing date is nobody's exception
    assert not any(
        c["table"] == "transactions" and c["label"] == "Entry" for c in ev["changes"]
    )
    # an account's balance moving with the posting is nobody's change
    assert not any(c["table"] == "accounts" for c in ev["changes"])


def test_each_override_is_its_own_event_with_its_own_rows(client, seed_accounts):
    _close(client)
    hdr = {"headers": {"X-Closing-Date-Password": quote(OVERRIDE_PW)}}
    assert (
        _journal(client, seed_accounts, "2026-02-01", "10.00", **hdr).status_code == 201
    )
    assert (
        _journal(client, seed_accounts, "2026-03-01", "20.00", **hdr).status_code == 201
    )
    data = _report(client)
    assert data["count"] == 2
    assert [ev["transaction_date"] for ev in data["events"]] == [
        "2026-02-01",
        "2026-03-01",
    ]
    for ev, amount in zip(data["events"], ("10.00", "20.00")):
        lines = [c for c in ev["changes"] if c["table"] == "transaction_lines"]
        assert len(lines) == 2, ev["changes"]
        assert any(
            f["field"] == "debit" and f["after"] == amount
            for c in lines
            for f in c["fields"]
        )


def test_the_report_saves_as_csv_and_pdf_and_as_a_saved_report(client, seed_accounts):
    _close(client)
    hdr = {"headers": {"X-Closing-Date-Password": quote(OVERRIDE_PW)}}
    assert _journal(client, seed_accounts, "2026-02-01", **hdr).status_code == 201
    csv = client.get(f"/api/reports/closing-date-exceptions/csv?{PERIOD}")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "Closing Date Exceptions" in csv.text
    assert "journal entry" in csv.text and "2026-06-30,2026-02-01" in csv.text
    assert "Overrides,1,Changes," in csv.text
    if WEASYPRINT_AVAILABLE:
        pdf = client.get(f"/api/reports/closing-date-exceptions/pdf?{PERIOD}")
        assert (
            pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
        )
    r = client.post(
        "/api/saved-reports",
        json={
            "name": "Exceptions",
            "report_type": "closing_date_exceptions",
            "parameters": {},
        },
    )
    assert r.status_code in (200, 201), r.text


def test_the_page_offers_the_report():
    reports_js = (ROOT / "app/static/js/reports.js").read_text(encoding="utf-8")
    assert "'closing-date-exceptions':" in reports_js
    assert "card('closing-date-exceptions', 'Closing Date Exceptions'" in reports_js
    assert "ReportsPage._exportButtons('closing-date-exceptions', qs)" in reports_js
