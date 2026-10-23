"""2.25.0: what the review of #363 (several companies on one server, #358)
asked for before it shipped.

- A company the server cannot open now answers 503 with the one sentence
  from the gate as well as from a route (it answered a traceback from the
  gate), and the sign-in status still lists the companies, so the screen
  keeps its choice (it lost it on a bodiless 503).
- The one-click switch looks at the person as they are in the other
  company now: a password reset or changed there since the sign-in, or a
  one-time password to replace, asks for that company's password instead.
- The setup wizard's encrypt box on a company served with the others leaves
  the started company's key alone: it read the desktop flag, which
  --serve-lan sets too, and took the desktop's recovery-key path.
- A company taken off the served list gives its engine up at once.
- Two people in two companies at the same time read their own books.
"""

import json
import threading

import pytest
from fastapi.testclient import TestClient

import app.database as db_module
from app.main import app
from app.services import company_file, served_companies
from tests.test_served_companies import (  # noqa: F401  (the fixture and helpers)
    A_PW,
    B_PW,
    _company_name,
    _new_company,
    _sign_in,
    served as served_fixture,
)


@pytest.fixture
def host(served_fixture):  # noqa: F811
    # Keith's fixture, registered under the name it was imported as
    """The Server Edition host with two companies, started for alpha.db."""
    return served_fixture


def _the_person_there(folder, filename, username, new_password=None, must_change=False):
    """What another company's administrator does to a person between the
    sign-in and the switch: a reset or a new password, a one-time password.
    Written through the app's own engine for that company, as the app does
    (a second SQLite library on an open WAL file is the NEW-156 trap)."""
    from app.database import use_company
    from app.models.users import User
    from app.services.auth import hash_password

    with use_company(filename):
        db = db_module.SessionLocal()
        try:
            user = db.query(User).filter(User.username == username).one()
            if new_password:
                user.password_hash = hash_password(new_password)
            user.must_change_password = must_change
            db.commit()
        finally:
            db.close()


def test_a_company_that_cannot_open_answers_the_sentence_and_the_sign_in_keeps_its_choice(
    host,
):
    client = TestClient(app)
    assert _sign_in(client, "lee", B_PW, company="bravo.db").status_code == 200
    assert _company_name(client) == "Bravo Food Pantry"
    db_module.forget_company_engines()  # as a restart, or an eviction, would
    (
        host / "companies" / "bravo.db"
    ).unlink()  # after the engine let go: Windows refuses otherwise
    r = client.get("/api/settings")
    assert r.status_code == 503, r.text
    assert "missing" in r.json()["detail"]
    status = client.get("/api/auth/status")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["authenticated"] is False
    assert "missing" in body["company_unavailable"]
    assert [c["file"] for c in body["companies"]] == ["alpha.db", "bravo.db"]
    # another company is a choice away, then its own sign-in
    assert (
        client.post("/api/auth/company", json={"file": "alpha.db"}).status_code == 200
    )
    assert client.get("/api/auth/status").json().get("company_unavailable") is None
    assert (
        client.post(
            "/api/auth/login", json={"username": "pat", "password": A_PW}
        ).status_code
        == 200
    )
    assert _company_name(client) == "Alpha Hardware"


def test_a_password_changed_in_the_other_company_stops_the_one_click_switch(host):
    client = TestClient(app)
    assert _sign_in(client, "pat", A_PW).status_code == 200
    assert client.get("/api/auth/status").json()["companies"][1]["opens"] is True
    # Bravo's administrator resets pat's password there in the meantime
    _the_person_there(
        host, "bravo.db", "pat", new_password="reset-by-lee-1", must_change=True
    )
    r = client.post("/api/auth/company", json={"file": "bravo.db"})
    assert r.status_code == 200, r.text
    assert r.json()["authenticated"] is False
    assert r.json()["username"] == "pat"  # Bravo's sign-in, with pat chosen
    # and the reset password, used, gets in with its one-time sentence
    r = client.post(
        "/api/auth/login", json={"username": "pat", "password": "reset-by-lee-1"}
    )
    assert r.status_code == 200, r.text
    assert client.get("/api/auth/status").json()["user"]["must_change_password"] is True


def test_a_one_time_password_pending_in_the_other_company_stops_the_click(host):
    client = TestClient(app)
    assert _sign_in(client, "pat", A_PW).status_code == 200
    _the_person_there(host, "bravo.db", "pat", must_change=True)
    r = client.post("/api/auth/company", json={"file": "bravo.db"})
    assert r.json()["authenticated"] is False
    # the same password, unchanged, still switches once the flag is gone
    _the_person_there(host, "bravo.db", "pat", must_change=False)
    assert _sign_in(client, "pat", A_PW, company="alpha.db").status_code == 200
    assert _company_name(client) == "Alpha Hardware"
    r = client.post("/api/auth/company", json={"file": "bravo.db"})
    assert r.json()["authenticated"] is True
    assert _company_name(client) == "Bravo Food Pantry"


def test_the_setup_wizards_encrypt_box_leaves_the_started_companys_key_alone(
    host, monkeypatch
):
    import app.routes.company_file as company_file_routes

    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")  # --serve-lan sets it too
    monkeypatch.delenv("COMPANY_FILE_ENCRYPTION_SECRET", raising=False)
    monkeypatch.delenv("SLOWBOOKS_COMPANY_FILE_KEY", raising=False)
    taken = []
    monkeypatch.setattr(
        company_file_routes,
        "turn_on_with_recovery_key",
        lambda *a, **k: taken.append(a) or "A-RECOVERY-KEY",
    )
    _new_company(host, "Charlie Cafe", "charlie.db")
    client = TestClient(app)
    assert _sign_in(client, "pat", A_PW).status_code == 200
    assert (
        client.post("/api/auth/company", json={"file": "charlie.db"}).json()["setup"]
        is True
    )
    r = client.post(
        "/api/auth/setup",
        json={
            "password": "charlie-password",
            "company_name": "Charlie Cafe",
            "encrypt": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("encrypted") is not True
    assert "COMPANY_FILE_ENCRYPTION_SECRET" in (body.get("encryption_note") or "")
    assert taken == []  # the desktop's recovery-key path was never taken
    assert (
        company_file.resolve_key() is None
    )  # the started company has no key put on it
    assert _company_name(client) == "Charlie Cafe"


def test_a_company_taken_off_the_list_gives_its_engine_up(host):
    client = TestClient(app)
    assert _sign_in(client, "lee", B_PW, company="bravo.db").status_code == 200
    assert "bravo.db" in db_module._company_engines
    manifest = host / "companies.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["companies"] = [c for c in data["companies"] if c["file"] != "bravo.db"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    served_companies.listed()  # reread on the next request that names a company
    assert "bravo.db" not in db_module._company_engines


def test_two_people_in_two_companies_at_the_same_time(host):
    a, b = TestClient(app), TestClient(app)
    assert _sign_in(a, "pat", A_PW).status_code == 200
    assert _sign_in(b, "lee", B_PW, company="bravo.db").status_code == 200
    seen = {"a": set(), "b": set()}
    errors = []

    def hammer(client, key):
        try:
            for _ in range(25):
                r = client.get("/api/settings")
                seen[key].add((r.status_code, r.json().get("company_name")))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=hammer, args=(a, "a")),
        threading.Thread(target=hammer, args=(b, "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert seen["a"] == {(200, "Alpha Hardware")}
    assert seen["b"] == {(200, "Bravo Food Pantry")}
