"""Invite an accountant, with an expiry (#345): an administrator makes a
sign-in with a one-time password, shown once with the address; the person
must set their own password at the first sign-in and can do nothing else
until then; a sign-in past its end date is refused with the date and can
be extended from the Users list."""

from datetime import date, timedelta
from pathlib import Path

from app.models.users import ROLE_BOOKKEEPER, User
from app.services import auth as auth_service

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PW = "test-password-123"


def _login(client, username, password):
    client.post("/api/auth/logout")
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _invite(client, **extra):
    r = client.post(
        "/api/users/invite",
        json={"username": "cpa", "display_name": "Pat CPA", **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_the_invitation_shows_the_password_once_and_the_first_sign_in_must_replace_it(
    client, db_session, seed_accounts
):
    inv = _invite(client)
    assert inv["role"] == "accountant" and inv["must_change_password"] is True
    assert inv["address"].startswith("http") and len(inv["password"]) >= 12
    one_time = inv["password"]
    # the list never shows it
    listed = next(u for u in client.get("/api/users").json() if u["username"] == "cpa")
    assert "password" not in listed and listed["must_change_password"] is True

    r = _login(client, "cpa", one_time)
    assert r.status_code == 200, r.text
    status = client.get("/api/auth/status").json()
    assert status["user"]["must_change_password"] is True
    # nothing else opens until the password is replaced
    r = client.get("/api/customers")
    assert r.status_code == 403 and "for one use" in r.json()["detail"]
    r = client.post("/api/auth/password", json={"new_password": "short"})
    assert r.status_code == 400
    r = client.post("/api/auth/password", json={"new_password": "my-own-password-9"})
    assert r.status_code == 200, r.text
    assert (
        client.get("/api/auth/status").json()["user"]["must_change_password"] is False
    )
    assert client.get("/api/customers").status_code == 200
    # the one-time password is spent; the chosen one works
    assert _login(client, "cpa", one_time).status_code == 401
    assert _login(client, "cpa", "my-own-password-9").status_code == 200
    listed = (
        next(u for u in client.get("/api/users").json() if u["username"] == "cpa")
        if False
        else None
    )
    user = db_session.query(User).filter(User.username == "cpa").one()
    assert user.must_change_password is False and user.last_login_at is not None


def test_a_sign_in_past_its_end_date_is_refused_with_the_date_and_can_be_extended(
    client, db_session, seed_accounts
):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    # the invitation refuses a day already past
    r = client.post(
        "/api/users/invite", json={"username": "cpa", "expires_on": yesterday}
    )
    assert r.status_code == 400 and "already past" in r.json()["detail"]
    inv = _invite(client, expires_on=tomorrow)
    assert inv["expires_on"] == tomorrow and inv["ended"] is False
    uid = inv["id"]
    # the administrator moves the end date to yesterday
    r = client.put(f"/api/users/{uid}", json={"expires_on": yesterday})
    assert r.status_code == 200 and r.json()["ended"] is True
    r = _login(client, "cpa", inv["password"])
    assert r.status_code == 403, r.text
    assert r.json()["detail"].startswith("This sign-in ended on ")
    assert "Settings → Users" in r.json()["detail"]
    # extended, it signs in; cleared, it has no end
    _login(client, "admin", ADMIN_PW)
    assert (
        client.put(f"/api/users/{uid}", json={"expires_on": tomorrow}).status_code
        == 200
    )
    assert _login(client, "cpa", inv["password"]).status_code == 200
    _login(client, "admin", ADMIN_PW)
    r = client.put(f"/api/users/{uid}", json={"expires_on": None})
    assert r.status_code == 200 and r.json()["expires_on"] is None


def test_only_an_administrator_invites_and_a_reset_is_one_time_too(
    client, db_session, seed_accounts
):
    db_session.add(
        User(
            username="keeper",
            display_name="Keeper",
            password_hash=auth_service.hash_password("keeper-password-1"),
            role=ROLE_BOOKKEEPER,
            is_active=True,
        )
    )
    db_session.commit()
    assert _login(client, "keeper", "keeper-password-1").status_code == 200
    assert client.post("/api/users/invite", json={"username": "cpa"}).status_code == 403
    # an administrator's reset hands out a one-time password as well
    _login(client, "admin", ADMIN_PW)
    keeper = next(
        u for u in client.get("/api/users").json() if u["username"] == "keeper"
    )
    r = client.put(f"/api/users/{keeper['id']}", json={"password": "reset-password-99"})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert _login(client, "keeper", "reset-password-99").status_code == 200
    assert client.get("/api/customers").status_code == 403
    # changing one's own password the ordinary way asks for the current one
    r = client.post("/api/auth/password", json={"new_password": "keeper-password-2"})
    assert r.status_code == 200  # the reset was one-time: no current needed
    r = client.post("/api/auth/password", json={"new_password": "keeper-password-3"})
    assert r.status_code == 403  # now it is
    r = client.post(
        "/api/auth/password",
        json={
            "current_password": "keeper-password-2",
            "new_password": "keeper-password-3",
        },
    )
    assert r.status_code == 200


def test_the_pages_carry_the_invitation_and_the_password_gate():
    settings = (ROOT / "app/static/js/settings.js").read_text(encoding="utf-8")
    assert 'onclick="SettingsPage.showInvite()">Invite your accountant…' in settings
    assert "API.post('/users/invite'" in settings
    assert "One-time password: ${r.password}" in settings
    assert '<th scope="col">Ends</th>' in settings
    assert "{expires_on: this.value || null}" in settings
    app = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    assert "if (App.mustChangePassword) {" in app
    assert (
        "API.post('/auth/password', { new_password: form.new_password.value })" in app
    )
    assert "App.mustChangePassword = true;" in app
