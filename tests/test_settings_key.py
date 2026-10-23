"""The key that encrypts saved passwords and API keys (2.18.0).

A Docker container had no lasting place for its settings key: with no
SETTINGS_ENCRYPTION_KEY, the key was a file written inside the container, so
recreating the container for an upgrade made a new one. Every saved secret
then failed to decrypt, and the error escaped every settings read: Settings,
invoice PDFs, email, the portal. Now the key is derived from the payroll
secret a Docker install must keep, and a secret no key here decrypts reads as
not set and is named on Settings to be entered again."""

import pytest
from cryptography.fernet import Fernet

from app.services import crypto

PAYROLL_PW = "a-real-payroll-secret-for-this-test"
OTHER_PAYROLL_PW = "the-next-payroll-secret-for-this-test"
# Throwaway saved settings: module constants, never a literal beside the field
OLD_SMTP_PW = "old-smtp-pw-for-this-test"
NEW_SMTP_PW = "new-smtp-pw-for-this-test"
OLD_STRIPE_PW = "old-stripe-pw-for-this-test"


@pytest.fixture
def fresh_keys(monkeypatch, tmp_path):
    """No settings key in the environment and no key file: a new Docker
    container. Restores the suite's own key afterwards."""
    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("PAYROLL_ENCRYPTION_SECRET_PREV", raising=False)
    monkeypatch.setenv("PAYROLL_ENCRYPTION_SECRET", PAYROLL_PW)
    key_file = tmp_path / ".slowbooks-master.key"
    monkeypatch.setattr(crypto, "_KEY_FILE", key_file)
    crypto.reset_cache_for_tests()
    yield key_file
    crypto.reset_cache_for_tests()


def test_a_recreated_container_still_reads_its_secrets(fresh_keys):
    stored = crypto.encrypt_value("smtp-password")
    assert crypto.key_source() == "derived"
    # the next container: nothing carried over but the environment
    if fresh_keys.exists():
        fresh_keys.unlink()
    crypto.reset_cache_for_tests()
    assert crypto.decrypt_value(stored) == "smtp-password"
    assert not fresh_keys.exists()


def test_the_placeholder_payroll_secret_is_never_a_key(fresh_keys, monkeypatch):
    monkeypatch.setenv(
        "PAYROLL_ENCRYPTION_SECRET", "slowbooks-dev-payroll-key-change-me"
    )
    crypto.reset_cache_for_tests()
    assert crypto.key_source() == "generated"
    assert fresh_keys.exists()


def test_an_install_with_a_key_keeps_it(fresh_keys, monkeypatch):
    own = Fernet.generate_key()
    fresh_keys.write_bytes(own)
    crypto.reset_cache_for_tests()
    assert crypto.key_source() == "file"
    assert (
        Fernet(own).decrypt(
            crypto.encrypt_value("x")[len(crypto.CIPHERTEXT_PREFIX) :].encode()
        )
        == b"x"
    )
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    crypto.reset_cache_for_tests()
    assert crypto.key_source() == "env"


def test_rotating_the_payroll_secret_keeps_the_settings_readable(
    fresh_keys, monkeypatch, client, seed_accounts, db_session
):
    from app.models.settings import Settings
    from app.services.encryption import rewrap_all

    assert (
        client.put("/api/settings", json={"smtp_password": OLD_SMTP_PW}).status_code
        == 200
    )
    monkeypatch.setenv("PAYROLL_ENCRYPTION_SECRET", OTHER_PAYROLL_PW)
    monkeypatch.setenv("PAYROLL_ENCRYPTION_SECRET_PREV", PAYROLL_PW)
    crypto.reset_cache_for_tests()
    assert client.get("/api/settings/unreadable-secrets").json()["keys"] == []
    summary = rewrap_all(db_session)
    assert summary["rewrapped"] == 1 and summary["failed"] == 0, summary
    monkeypatch.delenv("PAYROLL_ENCRYPTION_SECRET_PREV")
    crypto.reset_cache_for_tests()
    row = db_session.query(Settings).filter_by(key="smtp_password").one()
    db_session.refresh(row)
    assert crypto.decrypt_value(row.value) == OLD_SMTP_PW


def test_a_secret_no_key_decrypts_reads_as_not_set(client, seed_accounts, monkeypatch):
    assert (
        client.put(
            "/api/settings",
            json={"smtp_password": OLD_SMTP_PW, "stripe_secret_key": OLD_STRIPE_PW},
        ).status_code
        == 200
    )
    # the key it was saved under is gone
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    crypto.reset_cache_for_tests()
    try:
        r = client.get("/api/settings")
        assert r.status_code == 200, r.text
        assert r.json()["smtp_password"] == ""
        assert client.get("/api/settings/unreadable-secrets").json()["keys"] == [
            "smtp_password",
            "stripe_secret_key",
        ]
        # entering one again makes it readable, under the key this install has
        assert (
            client.put("/api/settings", json={"smtp_password": NEW_SMTP_PW}).status_code
            == 200
        )
        assert client.get("/api/settings/unreadable-secrets").json()["keys"] == [
            "stripe_secret_key"
        ]
    finally:
        crypto.reset_cache_for_tests()


def test_settings_names_what_to_enter_again():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "app/static/js/settings.js").read_text(
        encoding="utf-8"
    )
    assert "SettingsPage.loadUnreadableSecrets();" in js
    assert "API.get('/settings/unreadable-secrets')" in js
    for key in (
        "smtp_password",
        "stripe_secret_key",
        "simplefin_access_url",
        "closing_date_password",
        "ai_api_key",
    ):
        assert f"        {key}: '" in js, key
