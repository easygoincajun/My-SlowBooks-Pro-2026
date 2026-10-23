"""At-rest encryption for credential settings: SMTP, payments,
SimpleFIN, closing-date password. Redaction hides them from the API;
this layer hides them from the database file itself."""

from app.models.settings import Settings
from app.services.crypto import CIPHERTEXT_PREFIX, encrypt_value
from app.services.settings_service import (
    get_all_settings,
    get_setting_raw,
    set_setting,
    upgrade_plaintext_secrets,
)


def _raw_row(db, key):
    return db.query(Settings).filter(Settings.key == key).first()


def test_secret_settings_are_ciphertext_at_rest(client, db_session):
    r = client.put("/api/settings", json={"smtp_password": "hunter2-smtp"})
    assert r.status_code == 200
    row = _raw_row(db_session, "smtp_password")
    assert row.value.startswith(CIPHERTEXT_PREFIX)
    assert "hunter2-smtp" not in row.value
    # Service reads decrypt transparently
    assert get_setting_raw(db_session, "smtp_password") == "hunter2-smtp"
    assert get_all_settings(db_session)["smtp_password"] == "hunter2-smtp"
    # API still redacts
    assert client.get("/api/settings").json()["smtp_password"] == "********"


def test_non_secret_settings_stay_plaintext(client, db_session):
    client.put("/api/settings", json={"company_name": "Plain Co"})
    assert _raw_row(db_session, "company_name").value == "Plain Co"


def test_no_double_encryption(db_session):
    already = encrypt_value("once-only")
    set_setting(db_session, "stripe_secret_key", already)
    db_session.commit()
    stored = _raw_row(db_session, "stripe_secret_key").value
    assert stored == already  # not wrapped a second time
    assert get_setting_raw(db_session, "stripe_secret_key") == "once-only"


def test_legacy_plaintext_upgrades_at_boot(db_session):
    # Simulate a pre-upgrade install: plaintext secret in the table
    db_session.add(Settings(key="paypal_client_secret", value="legacy-plain"))
    db_session.add(Settings(key="company_phone", value="555-0100"))
    db_session.commit()

    n = upgrade_plaintext_secrets(db_session)
    assert n == 1  # only the secret row
    row = _raw_row(db_session, "paypal_client_secret")
    assert row.value.startswith(CIPHERTEXT_PREFIX)
    assert get_setting_raw(db_session, "paypal_client_secret") == "legacy-plain"
    assert _raw_row(db_session, "company_phone").value == "555-0100"
    # Idempotent
    assert upgrade_plaintext_secrets(db_session) == 0


def test_smtp_consumer_receives_plaintext(db_session):
    from app.services.email_service import _get_smtp_settings

    set_setting(db_session, "smtp_password", "smtp-secret-99")
    db_session.commit()
    assert _get_smtp_settings(db_session)["smtp_password"] == "smtp-secret-99"


def test_payments_loader_receives_plaintext(db_session):
    from app.services.payments import provider_settings, get_provider

    set_setting(db_session, "stripe_secret_key", "sk_test_abc123")
    db_session.commit()
    cfg = provider_settings(db_session, get_provider("stripe"))
    assert cfg["stripe_secret_key"] == "sk_test_abc123"


def test_closing_date_password_override_works_encrypted(db_session):
    from datetime import date, timedelta

    from app.services.closing_date import check_closing_date

    set_setting(db_session, "closing_date", date.today().isoformat())
    set_setting(db_session, "closing_date_password", "override-pw-1")
    db_session.commit()
    import pytest
    from fastapi import HTTPException

    locked_day = date.today() - timedelta(days=1)
    # Wrong/absent password: locked (raises)
    with pytest.raises(HTTPException):
        check_closing_date(db_session, locked_day)
    with pytest.raises(HTTPException):
        check_closing_date(db_session, locked_day, password="wrong")
    # Correct password decrypts and compares — no raise
    check_closing_date(db_session, locked_day, password="override-pw-1")


def test_simplefin_access_url_encrypted_via_existing_flow(client, db_session):
    set_setting(db_session, "simplefin_access_url", "https://u:p@bridge.example/x")
    db_session.commit()
    row = _raw_row(db_session, "simplefin_access_url")
    assert row.value.startswith(CIPHERTEXT_PREFIX)
    assert (
        get_setting_raw(db_session, "simplefin_access_url")
        == "https://u:p@bridge.example/x"
    )
