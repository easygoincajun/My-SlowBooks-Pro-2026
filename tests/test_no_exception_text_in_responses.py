"""CodeQL py/stack-trace-exposure (alerts 39, 45, 46, 47, 57): a route that
catches a bare Exception must not hand its text to the browser. The text
goes to the server log; the response says only that it failed."""

from tests.test_qb_report_import import FIXTURE

SECRET = "RuntimeError: /srv/secret/path.db is locked by pid 4242"


def _boom(*a, **kw):
    raise RuntimeError(SECRET)


def test_iif_import_does_not_echo_exception_text(client, monkeypatch, caplog):
    from app.routes import iif as route

    monkeypatch.setattr(route, "import_all", _boom)
    with caplog.at_level("ERROR"):
        r = client.post(
            "/api/iif/import", files={"file": ("x.iif", b"!HDR\n", "text/plain")}
        )
    assert (
        r.status_code == 500 and "secret" not in r.text and "server log" in r.text
    ), r.text
    assert "secret/path.db" in caplog.text


def test_qb_report_import_row_errors_keep_data_messages_but_not_crashes(
    client, seed_accounts, monkeypatch, caplog
):
    from app.services import qb_report_import as svc

    monkeypatch.setattr(svc, "Invoice", _boom)  # every receipt row now crashes
    with caplog.at_level("ERROR"):
        r = client.post(
            "/api/csv/import/qb-report",
            files={"file": ("receipts.csv", FIXTURE.read_bytes(), "text/csv")},
        )
    assert r.status_code == 200, r.text
    errors = r.json()["errors"]
    assert errors and all(
        "unexpected error" in e and "secret" not in e for e in errors
    ), errors
    # logged with its traceback (the message text is mangled by SQLAlchemy's
    # lambda wrapper in this seam, so assert on the shape, not the words)
    assert "Traceback" in caplog.text and "RuntimeError" in caplog.text


def test_donor_preview_reason_is_a_fixed_phrase_not_exception_text(
    client, seed_accounts, seed_customer
):
    """The one flagged site whose message was ours all along: it now comes
    off a typed reason, so the phrase still reaches the user."""
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-04-01",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": 40, "line_order": 0}],
        },
    )
    assert r.status_code == 201, r.text
    p = client.get(f"/api/donors/gifts/invoice/{r.json()['id']}/acknowledgment/preview")
    assert p.status_code == 200 and p.json() == {
        "eligible": False,
        "amount": None,
        "reason": "Acknowledge the payment, not the pledge",
    }, p.text
    pdf = client.get(f"/api/donors/gifts/invoice/{r.json()['id']}/acknowledgment/pdf")
    assert (
        pdf.status_code == 400
        and pdf.json()["detail"] == "Acknowledge the payment, not the pledge"
    )


# ---------------------------------------------------------------------------
# macbase1, 2.9.4 gate (HIGH): the IIF importer catches per ROW and answered
# 200 with the whole INSERT statement and every bound parameter — a live
# payment_token included — for an ordinary QuickBooks export missing its
# document number. The route-level handler never fires.
# ---------------------------------------------------------------------------

INVOICE_IIF_NO_DOCNUM = (
    "!TRNS\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tDUEDATE\tTERMS\tMEMO\n"
    "!SPL\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO\n"
    "!ENDTRNS\n"
    "TRNS\tINVOICE\t01/02/2026\tAccounts Receivable\tGate Customer\t1234.56\t\t01/02/2026\tNet 30\tno docnum\n"
    "SPL\tINVOICE\t01/02/2026\tSales\tGate Customer\t-1234.56\t\tline\n"
    "ENDTRNS\n"
)

SQL_MARKERS = (
    "INSERT INTO",
    "[SQL:",
    "[parameters:",
    "payment_token",
    "VALUES (",
    "Traceback",
)


def test_iif_row_errors_carry_the_constraint_not_the_statement(
    client, db_session, seed_accounts
):
    from app.models.contacts import Customer

    db_session.add(Customer(name="Gate Customer", is_active=True))
    db_session.commit()
    r = client.post(
        "/api/iif/import",
        files={"file": ("invoices.iif", INVOICE_IIF_NO_DOCNUM.encode(), "text/plain")},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert not any(m in body for m in SQL_MARKERS), body[:600]
    errors = r.json().get("errors") or []
    # the row is still reported, and the constraint is named — that is the
    # part a user can act on
    assert errors, r.text
    joined = " ".join(
        e.get("message", "") if isinstance(e, dict) else str(e) for e in errors
    )
    assert (
        "constraint" in joined.lower()
        or "invoice_number" in joined
        or "document" in joined.lower()
    ), joined


def test_safe_message_shapes(caplog):
    from sqlalchemy.exc import IntegrityError

    from app.services.safe_errors import GENERIC, DataProblem, safe_message

    class _Orig(Exception):
        pass

    ie = IntegrityError(
        "INSERT INTO invoices (...) VALUES (?, ?)",
        ("tok-secret", 1),
        _Orig("NOT NULL constraint failed: invoices.invoice_number"),
    )
    try:
        raise ie
    except IntegrityError as exc:
        msg = safe_message(exc, "test")
    assert (
        msg
        == "Database constraint: NOT NULL constraint failed: invoices.invoice_number"
    )
    assert "tok-secret" not in msg and "INSERT" not in msg
    # A sentence written for the user passes; Python's own wording does
    # not, because "is a ValueError" was never a property anyone could
    # check — a scanner sees a caught exception's text reaching a response.
    assert (
        safe_message(DataProblem("Row 3: amount is not a number"), "test")
        == "Row 3: amount is not a number"
    )
    with caplog.at_level("WARNING"):
        try:
            int("abc")
        except ValueError as exc:
            msg = safe_message(exc, "test")
    assert msg == GENERIC
    assert "invalid literal" in caplog.text  # logged, with the value
    with caplog.at_level("ERROR"):
        try:
            raise RuntimeError(SECRET)
        except RuntimeError as exc:
            msg = safe_message(exc, "test")
    assert msg == GENERIC and "secret" not in msg
    assert "secret/path.db" in caplog.text and "Traceback" in caplog.text


def test_test_email_does_not_echo_exception_text(client, monkeypatch):
    from app.routes import settings as settings_route

    monkeypatch.setattr(settings_route, "send_email", _boom, raising=False)
    r = client.post("/api/settings/test-email")
    assert r.status_code in (400, 500, 502) and "secret" not in r.text, r.text


def test_python_errors_are_bugs_not_user_messages(caplog):
    """macbase1, 2.9.4 round 2: KeyError / IndexError / ZeroDivisionError are
    what Python raises when the code is wrong; they must be logged as bugs
    and answered generically, never passed through as "'ACCNT'"."""
    from decimal import Decimal, InvalidOperation

    from app.services.safe_errors import GENERIC, safe_message

    cases = []
    for thrower in (lambda: {}["ACCNT"], lambda: [][3], lambda: 1 / 0):
        try:
            thrower()
        except Exception as exc:
            with caplog.at_level("ERROR"):
                cases.append(safe_message(exc, "test"))
    assert cases == [GENERIC, GENERIC, GENERIC]
    assert caplog.text.count("Traceback") >= 3
    try:
        Decimal("abc")
    except InvalidOperation as exc:
        assert safe_message(exc, "test") == "a number could not be read"


# ---------------------------------------------------------------------------
# CodeQL alerts 39, 45, 46, 47 again, 2.13.0. The four sites were already
# answering through safe_message, and the scanner was still right: for a
# ValueError it returned str(exc), and nothing distinguished "Missing
# customer NAME" from "invalid literal for int() with base 10: 'abc'". The
# marker is explicit now — user_text, set where the sentence is written.
# ---------------------------------------------------------------------------


def test_a_missing_control_account_tells_the_operator_what_to_restore(caplog):
    """LookupError is a bug — except this one, which was written for the
    operator and used to be swallowed into 'unexpected error'."""
    from app.services.control_accounts import MissingControlAccount
    from app.services.safe_errors import safe_message

    try:
        raise MissingControlAccount("1100", "Accounts Receivable", "every invoice")
    except LookupError as exc:
        msg = safe_message(exc, "test")
    assert msg.startswith("The account this posting needs is missing")
    assert "1100 Accounts Receivable" in msg and "Restore it" in msg


def test_qb_report_rows_keep_our_sentence_and_hide_pythons(
    client, seed_accounts, monkeypatch, caplog
):
    from app.services import inventory_hooks
    from app.services.safe_errors import DataProblem

    # a seam the row loop calls directly (the Invoice constructor sits
    # inside a SQLAlchemy lambda, which mangles what it raises)
    def _data_problem(*a, **kw):
        raise DataProblem("bank account 'Chase' not found")

    monkeypatch.setattr(inventory_hooks, "post_sale_for_invoice", _data_problem)
    r = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("receipts.csv", FIXTURE.read_bytes(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert all("bank account 'Chase' not found" in e for e in r.json()["errors"])

    def _key_error(*a, **kw):
        return {}["ACCNT"]

    monkeypatch.setattr(inventory_hooks, "post_sale_for_invoice", _key_error)
    with caplog.at_level("ERROR"):
        r = client.post(
            "/api/csv/import/qb-report",
            files={"file": ("receipts.csv", FIXTURE.read_bytes(), "text/csv")},
        )
    errors = r.json()["errors"]
    assert errors and all("unexpected error" in e for e in errors), errors
    assert "ACCNT" not in r.text and "KeyError" in caplog.text


def test_time_entry_post_to_job_answers_with_our_sentence_only(
    client, seed_accounts, monkeypatch, caplog
):
    emp = client.post(
        "/api/employees",
        json={
            "first_name": "Ann",
            "last_name": "Crew",
            "pay_type": "hourly",
            "pay_rate": 30,
            "cost_rate": 40,
        },
    )
    assert emp.status_code in (200, 201), emp.text
    te = client.post(
        "/api/time-entries",
        json={
            "employee_id": emp.json()["id"],
            "date": "2026-07-11",
            "hours_regular": 8,
        },
    )
    assert te.status_code == 201, te.text
    te_id = te.json()["id"]

    # a draft entry with no job: the service's own sentence
    r = client.post("/api/time-entries/post-to-job", json={"ids": [te_id]})
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]
    assert row["ok"] is False
    assert row["error"] in {
        "Time entry has no job",
        "Only submitted or approved time entries post to a job",
    }, row

    # Python's wording from somewhere underneath: not ours, not served
    from app.services import job_costing

    def _strptime_failure(db, entry):
        raise ValueError("time data '13/45/2026' does not match format '%m/%d/%Y'")

    monkeypatch.setattr(job_costing, "post_time_entry_to_job", _strptime_failure)
    with caplog.at_level("WARNING"):
        r = client.post("/api/time-entries/post-to-job", json={"ids": [te_id]})
    row = r.json()["results"][0]
    assert (
        row["ok"] is False
        and row["error"] == "unexpected error — the server log has the details"
    )
    assert "13/45/2026" not in r.text and "13/45/2026" in caplog.text
