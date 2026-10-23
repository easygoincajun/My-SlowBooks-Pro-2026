"""An editable email template cannot read a credential (GHSA-c3v4-f43f-4wqm).

Reported privately under SECURITY.md. `get_all_settings()` decrypts on the
way out, and the acknowledgment letter was rendered against the raw result —
so a template containing `{{ company.smtp_password }}` rendered the live SMTP
password, and `{{ company }}` dumped every credential at once, into an email
addressed to whoever the sender chose.

`GET /api/settings` redacts these keys for every role including admin, so the
intent has always been that they do not leave the server. Jinja's
`SandboxedEnvironment` is no help: `company` is a plain dict and reading a key
from it is an ordinary permitted operation, not a sandbox escape.

**It is privilege escalation.** A bookkeeper principal cannot write settings
and gets the redacted read, but can edit a template and trigger a send — so a
role specifically denied these values can mail them to an external address.

**Widened by this release before it was caught.** Issue #140 made the
`invoice_email` template actually render, having never been loaded by
anything. That took the exposure from nonprofit installs sending
acknowledgments to *every* install sending an invoice. These tests cover both
paths, and the redaction is applied where each context is built rather than at
the call sites.
"""

import pytest

from app.models.email_templates import EmailTemplate
from app.models.invoices import Invoice
from app.services.settings_service import (
    ENCRYPTED_SETTINGS_KEYS,
    SECRET_PLACEHOLDER,
    get_all_settings,
    redact_secrets,
    set_setting,
)

CANARIES = {
    "smtp_password": "hunter2-CANARY",
    "stripe_secret_key": "sk_live_CANARY",
    "square_access_token": "sq-CANARY",
    "simplefin_access_url": "https://CANARY@simplefin.example/x",
}


@pytest.fixture
def secrets(db_session):
    for k, v in CANARIES.items():
        set_setting(db_session, k, v)
    db_session.commit()
    return CANARIES


def _set_template(db_session, name, body):
    tpl = db_session.query(EmailTemplate).filter(EmailTemplate.name == name).first()
    if tpl is None:
        tpl = EmailTemplate(
            name=name, template_type="invoice", subject_template="x", body_template=body
        )
        db_session.add(tpl)
    else:
        tpl.body_template = body
    db_session.commit()


def test_redact_secrets_covers_every_encrypted_key():
    """The list is the one beside the encryption, so a credential added later
    is redacted by the same act that encrypts it."""
    raw = {k: "live-value" for k in ENCRYPTED_SETTINGS_KEYS}
    raw["company_name"] = "Acme"
    out = redact_secrets(raw)
    for k in ENCRYPTED_SETTINGS_KEYS:
        assert out[k] == SECRET_PLACEHOLDER, f"{k} was not redacted"
    assert out["company_name"] == "Acme", "ordinary settings must survive"


def test_an_empty_secret_stays_empty():
    """The UI tells an unconfigured value from a set one by whether it is
    blank; redacting an empty string would make everything look configured."""
    assert redact_secrets({"smtp_password": ""})["smtp_password"] == ""


def test_the_invoice_template_cannot_read_a_secret(
    client, db_session, seed_accounts, secrets
):
    """The path this release created. Without the fix this renders the live
    SMTP password into an email a customer receives."""
    from app.services.email_service import render_invoice_email_parts

    client.post("/api/email-templates/seed-defaults")
    _set_template(
        db_session,
        "invoice_email",
        "<p>{{ company.smtp_password }}|{{ company.stripe_secret_key }}</p>",
    )

    cust = client.post("/api/customers", json={"name": "P", "email": "a@b.com"}).json()
    iid = client.post(
        "/api/invoices",
        json={
            "customer_id": cust["id"],
            "date": "2026-06-01",
            "due_date": "2026-06-30",
            "lines": [
                {"description": "w", "quantity": 1, "rate": 100, "line_order": 0}
            ],
        },
    ).json()["id"]
    inv = db_session.query(Invoice).filter(Invoice.id == iid).first()

    _, body = render_invoice_email_parts(
        inv, get_all_settings(db_session), db=db_session
    )
    for value in secrets.values():
        assert value not in body, f"{value!r} reached the email body"
    assert SECRET_PLACEHOLDER in body


def test_dumping_the_whole_settings_dict_leaks_nothing(
    client, db_session, seed_accounts, secrets
):
    """`{{ company }}` was the worst case — every credential at once."""
    from app.services.email_service import render_invoice_email_parts

    client.post("/api/email-templates/seed-defaults")
    _set_template(db_session, "invoice_email", "<p>{{ company }}</p>")

    cust = client.post("/api/customers", json={"name": "P", "email": "a@b.com"}).json()
    iid = client.post(
        "/api/invoices",
        json={
            "customer_id": cust["id"],
            "date": "2026-06-01",
            "due_date": "2026-06-30",
            "lines": [
                {"description": "w", "quantity": 1, "rate": 100, "line_order": 0}
            ],
        },
    ).json()["id"]
    inv = db_session.query(Invoice).filter(Invoice.id == iid).first()

    _, body = render_invoice_email_parts(
        inv, get_all_settings(db_session), db=db_session
    )
    for value in secrets.values():
        assert value not in body, f"{value!r} reached the email body"


def test_the_redaction_matches_what_the_settings_api_shows(
    client, db_session, seed_accounts, secrets
):
    """The control this worked around. Both sides read one list now — this
    file used to carry a second frozenset identical to the first, which
    happened to agree and would not have said so if it stopped."""
    shown = client.get("/api/settings").json()
    redacted = redact_secrets(get_all_settings(db_session))
    for k in CANARIES:
        assert shown.get(k) == SECRET_PLACEHOLDER
        assert redacted[k] == SECRET_PLACEHOLDER


def test_the_two_secret_lists_are_one_object():
    """A credential added to one list and not the other would be encrypted at
    rest and rendered in plaintext by a template."""
    from app.routes import settings as settings_routes

    assert settings_routes.SECRET_KEYS is ENCRYPTED_SETTINGS_KEYS


def test_the_acknowledgment_template_cannot_read_a_secret(
    client, db_session, seed_accounts, secrets
):
    """The vector as originally reported — the donor acknowledgment letter.

    This one had been live since v2.9.0, on nonprofit installs. The invoice
    path above is the same hole widened to every install; both are closed at
    the point their context is built, which is why a third renderer added
    later inherits the fix rather than repeating the bug.
    """
    from app.services.donor_documents import ACK_TEMPLATE_NAME, render_acknowledgment

    _set_template(
        db_session,
        ACK_TEMPLATE_NAME,
        "<p>{{ company.smtp_password }}|{{ company.square_access_token }}|{{ company }}</p>",
    )
    cust = client.post(
        "/api/customers", json={"name": "Donor", "email": "d@e.com"}
    ).json()
    from app.models.contacts import Customer

    customer = db_session.query(Customer).filter(Customer.id == cust["id"]).first()

    from datetime import date as _date

    gift = {
        "id": 1,
        "number": "G-1",
        "date": _date(2026, 6, 1),
        "amount": 100,
        "description": "",
        "fair_value_amount": None,
        "fair_value_description": None,
        "in_kind_lines": [],
        "customer_id": customer.id,
    }
    subject, body = render_acknowledgment(
        db_session, get_all_settings(db_session), customer, gift
    )
    for value in secrets.values():
        assert value not in body, f"{value!r} reached the acknowledgment body"
        assert value not in subject, f"{value!r} reached the acknowledgment subject"
