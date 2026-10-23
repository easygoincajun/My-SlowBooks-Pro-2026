"""A processed pay run's direct-deposit file, from the page (discussion #211).

The NACHA file could only be made by an API client that sent the bank's
numbers in the request body; the page had no button and nothing kept the
numbers. Settings → Direct Deposit (ACH) keeps them now, GET
/api/payroll/{run_id}/nacha builds the file from them, and the pay run
says who the file pays and who is paid by check.
"""

import re
import shutil
import subprocess
import json
from pathlib import Path

import pytest

from app.routes.settings import SECRET_PLACEHOLDER
from tests.test_api_tokens import _bearer, _mint

ROOT = Path(__file__).resolve().parents[1]
PAYROLL_JS = (ROOT / "app" / "static" / "js" / "payroll.js").read_text(encoding="utf-8")
SETTINGS_JS = (ROOT / "app" / "static" / "js" / "settings.js").read_text(
    encoding="utf-8"
)

BANK = {
    "ach_bank_routing": "021000021",
    "ach_bank_name": "First Example Bank",
    "ach_company_account": "123456789",
}


def _employee(client, **overrides):
    body = {
        "first_name": "Pat",
        "last_name": "Worker",
        "pay_type": "salary",
        "pay_rate": 52000,
        "pay_frequency": "biweekly",
        "filing_status": "single",
        "work_state": "WA",
    }
    body.update(overrides)
    r = client.post("/api/employees", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _bank_account(client, emp_id, **overrides):
    body = {
        "account_kind": "checking",
        "routing_number": "121000248",
        "account_number": "9876543210",
        "deposit_type": "full",
    }
    body.update(overrides)
    r = client.post(f"/api/employees/{emp_id}/bank-accounts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _run(client, *employees, process=True):
    r = client.post(
        "/api/payroll",
        json={
            "period_start": "2026-04-01",
            "period_end": "2026-04-15",
            "pay_date": "2026-04-20",
            "stubs": [{"employee_id": e["id"]} for e in employees],
        },
    )
    assert r.status_code == 201, r.text
    run = r.json()
    if process:
        assert client.post(f"/api/payroll/{run['id']}/process").status_code == 200
    return run


def _lines(text):
    lines = text.rstrip("\n").split("\n")
    assert all(len(line) == 94 for line in lines), [len(line) for line in lines]
    return lines


# --- Settings ---------------------------------------------------------------


def test_the_bank_details_are_saved_and_the_account_is_a_secret(client, db_session):
    r = client.put("/api/settings", json={**BANK, "ach_immediate_origin": "1123456789"})
    assert r.status_code == 200, r.text
    got = client.get("/api/settings").json()
    assert got["ach_bank_routing"] == "021000021"
    assert got["ach_bank_name"] == "First Example Bank"
    assert got["ach_immediate_origin"] == "1123456789"
    # the account number never comes back, and sits encrypted in the file
    assert got["ach_company_account"] == SECRET_PLACEHOLDER
    from app.models.settings import Settings

    row = db_session.query(Settings).filter_by(key="ach_company_account").first()
    assert row.value.startswith("fernet:")
    # saving the page again with the placeholder keeps the real number
    assert (
        client.put(
            "/api/settings", json={"ach_company_account": SECRET_PLACEHOLDER}
        ).status_code
        == 200
    )
    from app.services.settings_service import get_all_settings

    assert get_all_settings(db_session)["ach_company_account"] == "123456789"


@pytest.mark.parametrize("bad", ["12345", "021000022", "abc", "0210000210"])
def test_a_routing_number_that_is_not_one_is_refused(client, bad):
    r = client.put("/api/settings", json={"ach_bank_routing": bad})
    assert r.status_code == 422, r.text
    assert "9-digit ABA routing number" in r.json()["detail"]
    assert client.get("/api/settings").json()["ach_bank_routing"] == ""


@pytest.mark.parametrize("key", ["ach_immediate_origin", "ach_company_id"])
def test_an_identifier_longer_than_its_field_is_refused(client, key):
    r = client.put("/api/settings", json={key: "12345678901"})
    assert r.status_code == 422, r.text
    assert "up to 10 letters or digits" in r.json()["detail"]
    assert client.put("/api/settings", json={key: "1AB2345678"}).status_code == 200


def test_the_settings_page_has_the_fields():
    assert 'id="settings-h-direct-deposit"' in SETTINGS_JS
    for name in (
        "ach_bank_routing",
        "ach_bank_name",
        "ach_company_account",
        "ach_immediate_origin",
        "ach_company_id",
    ):
        assert f'name="{name}"' in SETTINGS_JS, name
    # the account number is typed into a password box
    assert re.search(r'name="ach_company_account" type="password"', SETTINGS_JS)


# --- The file ---------------------------------------------------------------


def test_the_file_downloads_from_the_saved_settings(client, seed_accounts):
    client.put(
        "/api/settings",
        json={**BANK, "company_name": "Example Co", "company_tax_id": "12-3456789"},
    )
    emp = _employee(client)
    _bank_account(client, emp["id"])
    run = _run(client, emp)
    net = run["stubs"][0]["net_pay"]

    r = client.get(f"/api/payroll/{run['id']}/nacha")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].startswith("attachment")
    assert (
        'filename="Direct-Deposit_2026-04-20.ach"' in r.headers["content-disposition"]
    )
    lines = _lines(r.text)
    assert len(lines) % 10 == 0
    header, batch = lines[0], lines[1]
    assert header[0] == "1"
    assert header[3:13] == " 021000021"  # the bank, as immediate destination
    assert header[13:23] == "1123456789"  # "1" + EIN, from the tax id, all 10
    assert header[40:63].rstrip() == "First Example Bank"
    assert header[63:86].rstrip() == "Example Co"
    assert batch[0] == "5"
    assert batch[4:20].rstrip() == "Example Co"
    assert batch[40:50] == "1123456789"  # company identification
    assert batch[69:75] == "260420"  # effective date: the pay date
    assert batch[79:87] == "02100002"  # originating DFI: the bank's prefix
    credits = [line for line in lines if line.startswith("622")]
    assert len(credits) == 1
    assert credits[0][3:12] == "121000248"
    assert credits[0][12:29].rstrip() == "9876543210"
    assert int(credits[0][29:39]) == round(net * 100)
    assert credits[0][54:76].rstrip() == "Pat Worker"
    debits = [line for line in lines if line.startswith("627")]
    assert len(debits) == 1
    assert debits[0][3:12] == "021000021"
    assert debits[0][12:29].rstrip() == "123456789"
    assert int(debits[0][29:39]) == round(net * 100)


def test_without_the_bank_details_the_download_says_what_to_enter(
    client, seed_accounts
):
    emp = _employee(client)
    _bank_account(client, emp["id"])
    run = _run(client, emp)
    r = client.get(f"/api/payroll/{run['id']}/nacha")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Bank routing number and Company account number" in detail
    assert "Settings → Direct Deposit (ACH)" in detail
    # one of the two saved: the other is named alone
    client.put("/api/settings", json={"ach_bank_routing": "021000021"})
    detail = client.get(f"/api/payroll/{run['id']}/nacha").json()["detail"]
    assert "enter Company account number under" in detail


def test_a_draft_run_has_no_file(client, seed_accounts):
    client.put("/api/settings", json=BANK)
    emp = _employee(client)
    _bank_account(client, emp["id"])
    run = _run(client, emp, process=False)
    r = client.get(f"/api/payroll/{run['id']}/nacha")
    assert r.status_code == 400
    assert "processed" in r.json()["detail"]
    assert client.get("/api/payroll/999/nacha").status_code == 404


def test_each_file_made_is_in_the_audit_trail(client, db_session, seed_accounts):
    # A file that moves the payroll is a record of its own: who made it, for
    # which run, how many it pays and how much — never the account numbers.
    from app.models.audit import AuditLog

    def exports():
        return (
            db_session.query(AuditLog)
            .filter(AuditLog.table_name == "pay_runs", AuditLog.action == "EXPORT")
            .all()
        )

    emp = _employee(client)
    _bank_account(client, emp["id"])
    run = _run(client, emp)
    # refused (no bank details yet): no file, so no row
    assert client.get(f"/api/payroll/{run['id']}/nacha").status_code == 409
    assert exports() == []

    client.put("/api/settings", json=BANK)
    assert client.get(f"/api/payroll/{run['id']}/nacha").status_code == 200
    assert client.post(f"/api/payroll/{run['id']}/nacha", json={}).status_code == 200
    rows = exports()
    assert len(rows) == 2
    row = rows[0]
    assert row.record_id == run["id"]
    assert row.new_values["file"] == "Direct-Deposit_2026-04-20.ach"
    assert row.new_values["credits"] == 1
    assert row.new_values["total"] == f"{run['stubs'][0]['net_pay']:.2f}"
    assert row.new_values["effective_date"] == "2026-04-20"
    logged = json.dumps([r.new_values for r in rows])
    assert "9876543210" not in logged and "123456789" not in logged


def test_the_audit_page_shows_and_filters_an_export():
    audit_js = (ROOT / "app" / "static" / "js" / "audit.js").read_text(encoding="utf-8")
    assert '<option value="EXPORT">EXPORT</option>' in audit_js
    # a hand-logged event (EXPORT, RESTORE) lists what it recorded
    assert "} else if (log.new_values) {" in audit_js


def test_the_run_says_who_the_file_pays(client, seed_accounts):
    client.put("/api/settings", json=BANK)
    deposit = _employee(client, first_name="Dee", last_name="Posit")
    _bank_account(client, deposit["id"])
    check = _employee(client, first_name="Chet", last_name="Byczek")
    waiting = _employee(client, first_name="Pre", last_name="Note")
    _bank_account(client, waiting["id"], account_number="5555")
    run = _run(client, deposit, check, waiting, process=False)

    got = client.get(f"/api/payroll/{run['id']}").json()
    by_name = {s["employee_name"]: s["direct_deposit"] for s in got["stubs"]}
    assert by_name == {"Dee Posit": True, "Chet Byczek": False, "Pre Note": True}

    # the list answers the same
    listed = {
        s["employee_name"]: s["direct_deposit"]
        for r in client.get("/api/payroll").json()
        if r["id"] == run["id"]
        for s in r["stubs"]
    }
    assert listed == by_name

    client.post(f"/api/payroll/{run['id']}/process")
    r = client.get(f"/api/payroll/{run['id']}/nacha")
    assert r.status_code == 200, r.text
    credits = [line for line in _lines(r.text) if line.startswith("622")]
    assert sorted(c[54:76].rstrip() for c in credits) == ["Dee Posit", "Pre Note"]


def test_an_account_waiting_on_its_prenote_is_left_out(
    client, db_session, seed_accounts
):
    client.put("/api/settings", json=BANK)
    emp = _employee(client, first_name="Pre", last_name="Note")
    acct = _bank_account(client, emp["id"])
    from app.models.bank_accounts import EmployeeBankAccount, PrenoteStatus

    row = db_session.query(EmployeeBankAccount).filter_by(id=acct["id"]).first()
    row.prenote_status = PrenoteStatus.PENDING
    db_session.commit()
    run = _run(client, emp)
    got = client.get(f"/api/payroll/{run['id']}").json()
    assert got["stubs"][0]["direct_deposit"] is False
    r = client.get(f"/api/payroll/{run['id']}/nacha")
    assert r.status_code == 409, r.text
    assert "No one on this pay run is paid by direct deposit" in r.json()["detail"]


def test_a_run_with_no_accounts_is_refused_not_an_empty_file(client, seed_accounts):
    client.put("/api/settings", json=BANK)
    emp = _employee(client)
    run = _run(client, emp)
    r = client.get(f"/api/payroll/{run['id']}/nacha")
    assert r.status_code == 409, r.text
    assert "Pay them by check" in r.json()["detail"]


def test_the_generator_itself_refuses_a_file_with_no_credits(
    client, db_session, seed_accounts
):
    """The route checks first and says what to do; the generator checks
    too, for every caller, so an empty but well-formed file never leaves
    (a bank would take it as a batch of nothing). From PNWImport's fork."""
    from app.services.nacha_export import generate_nacha_file

    emp = _employee(client)
    run = _run(client, emp)
    orig = {
        "immediate_destination": "021000021",
        "immediate_origin": "1123456789",
        "originating_dfi_id": "02100002",
        "company_account": "123456789",
        "company_name": "Example Co",
        "company_id": "1123456789",
    }
    with pytest.raises(ValueError, match="nothing to send to the bank"):
        generate_nacha_file(db_session, run["id"], orig)
    # and with one account, the file comes back whole
    _bank_account(client, emp["id"])
    db_session.expire_all()
    lines = _lines(generate_nacha_file(db_session, run["id"], orig))
    assert len([ln for ln in lines if ln.startswith("622")]) == 1


def test_a_posted_body_outranks_the_saved_settings(client, seed_accounts):
    client.put("/api/settings", json={**BANK, "company_name": "Example Co"})
    emp = _employee(client)
    _bank_account(client, emp["id"])
    run = _run(client, emp)
    # an empty body is the saved settings
    r = client.post(f"/api/payroll/{run['id']}/nacha")
    assert r.status_code == 200, r.text
    assert _lines(r.text)[0][63:86].rstrip() == "Example Co"
    # a field sent is used in place of the saved one; the rest stay saved
    r = client.post(
        f"/api/payroll/{run['id']}/nacha",
        json={"company_name": "Example Payroll", "effective_date": "2026-04-22"},
    )
    assert r.status_code == 200, r.text
    lines = _lines(r.text)
    assert lines[1][4:20].rstrip() == "Example Payroll"
    assert lines[1][69:75] == "260422"
    assert lines[0][3:13] == " 021000021"
    # the whole body, as before 2.26, still works with nothing saved
    client.put(
        "/api/settings", json={"ach_bank_routing": "", "ach_company_account": ""}
    )
    assert client.get(f"/api/payroll/{run['id']}/nacha").status_code == 409
    r = client.post(
        f"/api/payroll/{run['id']}/nacha",
        json={
            "immediate_destination": "021000021",
            "immediate_origin": "1123456789",
            "originating_dfi_id": "02100002",
            "company_account": "123456789",
        },
    )
    assert r.status_code == 200, r.text


def test_a_bookkeeper_cannot_download_the_file(client, seed_accounts):
    client.put("/api/settings", json=BANK)
    emp = _employee(client)
    _bank_account(client, emp["id"])
    run = _run(client, emp)
    keeper = _bearer(_mint(client, "agent-bk", role="bookkeeper")["token"])
    assert keeper.get(f"/api/payroll/{run['id']}/nacha").status_code == 403
    assert keeper.put("/api/settings", json=BANK).status_code == 403


# --- The page ---------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pay_run_page_offers_the_file_to_a_processed_run_with_deposits():
    out = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "direct_deposit_probe.js")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    ).stdout
    got = json.loads(out)
    # processed, one by deposit and one by check: the button and the count
    assert got["processed"]["button"] is True
    assert (
        "Direct deposit file: 1 employee; 1 is paid by check"
        in got["processed"]["note"]
    )
    assert got["processed"]["paid_by"] == ["Direct deposit", "Check"]
    # processed, nobody by deposit: no button, told to pay by check
    assert got["checks_only"]["button"] is False
    assert "pay each stub by check" in got["checks_only"]["note"]
    # a draft: no button yet
    assert got["draft"]["button"] is False
    assert "Once processed" in got["draft"]["note"]
