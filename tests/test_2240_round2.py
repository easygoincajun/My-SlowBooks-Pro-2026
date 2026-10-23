"""2.24.0 round 2: what the gate's first round found (skytech W-18 to W-22,
macbase1 NEW-148 to NEW-164), each pinned here.

- NEW-156 (critical): one SQLite library per process on a company file,
  so a backup or an accountant's copy never deletes the WAL under the
  engine.
- NEW-157: the key set at runtime (a new recovery key) wins over the one
  the launcher handed on.
- NEW-148: hashes and secret settings never in the audit log: written
  redacted, read redacted, the package and the copy redacted.
- W-18 / NEW-149: a sign-in that must set its password still reads the
  version and the company's words, and the shell draws the form.
- NEW-150: the invitation's address is never loopback alone.
- NEW-152 / W-19 / NEW-153: Closing Date Exceptions on the person's clock,
  accounts by name, the plural right.
- NEW-154: the package's audit range stays inside the period.
- NEW-155, NEW-160, NEW-161: no look-alikes in the passphrase; a recovery
  key typed with 0/1/8 reads; the key file dated locally.
- NEW-158: the backups list says which backups are plain.
- The accountant and the employee directory; Turn off asks the password.
- The pages: W-20, W-21, W-22, NEW-151, NEW-163, NEW-164 (source pins; the
  browser run is tests/test_browser_open_tabs.py).
"""

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path


from app.services import company_file, company_keyring
from app.services.audit_redaction import REDACTED, redact_values

ROOT = Path(__file__).resolve().parents[1]
PW = "test-password-123"  # the suite's administrator (tests/conftest.py)


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# NEW-156: one SQLite library on a company file
# ---------------------------------------------------------------------------


def test_the_backup_source_opens_through_the_engines_own_sqlite(tmp_path):
    """The engine opens every plain file through sqlcipher3; a backup or a
    copy that opened the same file through the standard module closed as
    the "last" connection its library knew of, checkpointed and deleted
    the WAL under the engine, and the engine's later writes were lost at
    exit (every release from 2.23.2). Both go through one library now, and
    a write after a backup survives the engine's close."""
    from sqlalchemy import text

    path = tmp_path / "books.db"
    engine = company_file.create_sqlite_engine("sqlite:///" + path.as_posix())
    from app.database import enable_sqlite_tuning

    enable_sqlite_tuning(engine)  # WAL, as the app runs
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
    # the backup's source connection, as backup_service makes it
    src = company_file.connect_raw(path, None)
    assert (
        type(src).__module__.split(".")[0]
        == type(engine.raw_connection().dbapi_connection).__module__.split(".")[0]
    ), "two SQLite libraries on one file"
    dest = company_file.connect_raw(tmp_path / "backup.db", None)
    src.backup(dest)
    dest.close()
    src.close()
    # a write after the backup, then the engine goes away as the server does
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO t VALUES (2)"))
    engine.dispose()
    # read back with whatever library: both rows are in the file
    again = company_file.connect_raw(path, None)
    try:
        rows = [r[0] for r in again.execute("SELECT x FROM t ORDER BY x")]
    finally:
        again.close()
    assert rows == [1, 2]


def test_every_company_file_read_uses_the_one_library():
    """Nothing opens a company file with the standard module beside the
    engine: the copy's work files and the legacy sweep included."""
    for rel in ("app/services/accountants_copy.py", "app/services/legacy_uploads.py"):
        src = _src(rel)
        assert "sqlite3.connect(" not in src, rel
    assert "def sqlite_module()" in _src("app/services/company_file.py")
    src = _src("app/services/backup_service.py")
    assert "sqlite3.connect(" not in src


# ---------------------------------------------------------------------------
# NEW-157: the runtime key wins
# ---------------------------------------------------------------------------


def test_a_key_set_at_runtime_wins_over_the_launchers(monkeypatch):
    old, new = bytes(range(32)), bytes(range(32, 64))
    monkeypatch.setenv(company_file.KEY_ENV, old.hex())
    monkeypatch.delenv(company_file.SECRET_ENV, raising=False)
    company_file.set_runtime_key(None)
    try:
        assert company_file.resolve_key() == old
        company_file.set_runtime_key(new)  # a new recovery key re-keyed the file
        assert company_file.resolve_key() == new
    finally:
        company_file.set_runtime_key(None)


def test_the_engine_looks_the_key_up_again_after_a_re_key(tmp_path, monkeypatch):
    from sqlalchemy import text

    monkeypatch.delenv(company_file.KEY_ENV, raising=False)
    monkeypatch.delenv(company_file.SECRET_ENV, raising=False)
    old, new = bytes(range(32)), bytes(range(32, 64))
    path = tmp_path / "books.db"
    conn = company_file.connect_raw(path, old)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    conn.close()
    company_file.set_runtime_key(old)
    try:
        engine = company_file.create_sqlite_engine(
            "sqlite:///" + path.as_posix(), company_file.resolve_key
        )
        with engine.connect() as c:
            assert c.execute(text("SELECT x FROM t")).scalar() == 7
        engine.dispose()
        company_file.rekey(path, old, new)
        company_file.set_runtime_key(new)
        with engine.connect() as c:
            assert c.execute(text("SELECT x FROM t")).scalar() == 7
        engine.dispose()
    finally:
        company_file.set_runtime_key(None)


# ---------------------------------------------------------------------------
# NEW-148: no hashes or secrets in the audit log
# ---------------------------------------------------------------------------


def test_redaction_covers_hashes_and_secret_settings():
    out = redact_values(
        "users",
        {"username": "cpa", "password_hash": "$argon2id$x", "role": "accountant"},
    )
    assert out["password_hash"] == REDACTED and out["username"] == "cpa"
    out = redact_values(
        "settings", {"key": "auth_password_hash", "value": "$argon2id$x"}
    )
    assert out["value"] == REDACTED
    out = redact_values(
        "settings", {"key": "closing_date_password", "value": "fernet:v1:abc"}
    )
    assert out["value"] == REDACTED
    out = redact_values("settings", {"key": "company_session_id", "value": "deadbeef"})
    assert out["value"] == REDACTED
    out = redact_values("settings", {"key": "accountants_copy_token", "value": "tok"})
    assert out["value"] == REDACTED
    out = redact_values("settings", {"key": "company_name", "value": "Harbor Light"})
    assert out["value"] == "Harbor Light"
    out = redact_values(
        "employee_bank_accounts", {"routing_number_enc": "x", "nickname": "n"}
    )
    assert out["routing_number_enc"] == REDACTED and out["nickname"] == "n"


def test_a_password_change_and_a_secret_setting_are_logged_redacted(client, db_session):
    from app.models.audit import AuditLog
    from app.models.users import ROLE_ACCOUNTANT

    r = client.post(
        "/api/users/invite", json={"username": "cpa", "role": ROLE_ACCOUNTANT}
    )
    assert r.status_code == 201, r.text
    one_time = r.json()["password"]
    client.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login", json={"username": "cpa", "password": one_time}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/password", json={"new_password": "cpa-own-password-1"}
        ).status_code
        == 200
    )
    client.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login", json={"username": "admin", "password": PW}
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/settings",
            json={"closing_date": "2026-02-28", "closing_date_password": "shut-tight"},
        ).status_code
        == 200
    )
    for row in db_session.query(AuditLog).all():
        blob = json.dumps([row.old_values, row.new_values])
        assert "$argon2" not in blob, (row.table_name, row.action)
        assert "fernet:v1" not in blob, (row.table_name, row.action)
        assert "shut-tight" not in blob
    # the UPDATE of cpa's password carries the field, not the hash
    changed = [
        row
        for row in db_session.query(AuditLog)
        .filter_by(table_name="users", action="UPDATE")
        .all()
        if "password_hash" in (row.changed_fields or [])
    ]
    assert changed and all(
        row.old_values["password_hash"] == REDACTED
        and row.new_values["password_hash"] == REDACTED
        for row in changed
    )


def test_the_audit_api_redacts_rows_written_before(client, db_session):
    from app.models.audit import AuditLog

    db_session.add(
        AuditLog(
            table_name="users",
            record_id=9,
            action="UPDATE",
            old_values={"password_hash": "$argon2id$old"},
            new_values={"password_hash": "$argon2id$new"},
            changed_fields=["password_hash"],
            source="api",
            username="admin",
        )
    )
    db_session.add(
        AuditLog(
            table_name="settings",
            record_id=3,
            action="INSERT",
            old_values=None,
            new_values={"key": "auth_password_hash", "value": "$argon2id$x"},
            changed_fields=["key", "value"],
            source="api",
            username="admin",
        )
    )
    db_session.commit()
    rows = client.get("/api/audit?table_name=users").json()
    mine = next(r for r in rows if r["record_id"] == 9)
    assert mine["old_values"]["password_hash"] == REDACTED
    assert mine["new_values"]["password_hash"] == REDACTED
    rows = client.get("/api/audit?table_name=settings").json()
    mine = next(r for r in rows if r["record_id"] == 3)
    assert (
        mine["new_values"]["value"] == REDACTED
        and mine["new_values"]["key"] == "auth_password_hash"
    )


def test_the_package_and_the_exceptions_report_redact_too(db_session, seed_accounts):
    from app.models.audit import AuditLog
    from app.routes.reports.package import audit_csv
    from app.services.closing_date_exceptions import fields_of

    row = AuditLog(
        table_name="users",
        record_id=9,
        action="UPDATE",
        old_values={"password_hash": "$argon2id$old"},
        new_values={"password_hash": "$argon2id$new"},
        changed_fields=["password_hash"],
        source="api",
        username="admin",
        timestamp=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    fields = fields_of(row)
    assert fields == [{"field": "password_hash", "before": REDACTED, "after": REDACTED}]
    text = audit_csv([row], "Harbor Light", "2026-01-01", "2026-12-31")
    assert "$argon2" not in text and REDACTED in text


def test_the_copy_carries_a_redacted_audit_log(tmp_path):
    """_strip_owner rewrites the copy's audit_log through the same list."""
    from app.services.accountants_copy import _strip_owner

    plain = tmp_path / "copy.db"
    conn = company_file.connect_raw(plain, None)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)"
    )
    conn.execute(
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, table_name TEXT, record_id INTEGER, "
        "action TEXT, old_values TEXT, new_values TEXT)"
    )
    conn.execute(
        "INSERT INTO audit_log VALUES (1, 'users', 2, 'UPDATE', ?, ?)",
        (
            json.dumps({"password_hash": "$argon2id$old"}),
            json.dumps({"password_hash": "$argon2id$new"}),
        ),
    )
    conn.execute(
        "INSERT INTO audit_log VALUES (2, 'settings', 3, 'INSERT', NULL, ?)",
        (json.dumps({"key": "closing_date_password", "value": "fernet:v1:abc"}),),
    )
    conn.execute(
        "INSERT INTO audit_log VALUES (3, 'invoices', 4, 'INSERT', NULL, ?)",
        (json.dumps({"invoice_number": "INV-1", "total": "10.00"}),),
    )
    conn.commit()
    conn.close()
    _strip_owner(
        plain,
        {
            "copy_id": "c" * 36,
            "token": "t" * 64,
            "dividing_date": "2026-03-31",
            "made_at": "2026-10-20T00:00:00+00:00",
            "company_name": "Harbor Light",
            "last_txn_id": 0,
        },
    )
    conn = company_file.connect_raw(plain, None)
    try:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT id, old_values, new_values FROM audit_log")
        }
    finally:
        conn.close()
    assert "$argon2" not in json.dumps(rows) and "fernet:v1" not in json.dumps(rows)
    assert json.loads(rows[1][1])["password_hash"] == REDACTED
    assert json.loads(rows[2][1])["value"] == REDACTED
    assert (
        json.loads(rows[3][1])["invoice_number"] == "INV-1"
    )  # the books' own rows untouched


# ---------------------------------------------------------------------------
# W-18 / NEW-149: the password page can be drawn
# ---------------------------------------------------------------------------


def test_a_one_time_sign_in_reads_the_version_and_the_words_and_nothing_else(
    client, db_session
):
    from app.models.users import ROLE_ACCOUNTANT

    r = client.post(
        "/api/users/invite", json={"username": "cpa", "role": ROLE_ACCOUNTANT}
    )
    one_time = r.json()["password"]
    client.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login", json={"username": "cpa", "password": one_time}
        ).status_code
        == 200
    )
    assert client.get("/api/system").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/auth/status").json()["user"]["must_change_password"] is True
    assert client.get("/api/customers").status_code == 403
    assert client.put("/api/settings", json={"company_name": "x"}).status_code == 403


def test_the_shell_reads_the_sign_in_even_when_the_version_is_refused():
    src = _src("app/static/js/app.js")
    start = src.index("async initSystemInfo() {")
    init = src[start : src.index("\n    },\n", start)]
    # nothing returns before the sign-in's state is read (the update check,
    # later in the function, may)
    assert "if (!res.ok) return;" not in init[: init.index("/api/auth/status")]
    assert "const info = res.ok ? await res.json() : {};" in init
    assert "App.mustChangePassword = true;" in init


# ---------------------------------------------------------------------------
# NEW-150: the invitation's address
# ---------------------------------------------------------------------------


def test_the_invitations_address_is_never_loopback_alone(client, monkeypatch):
    from app.models.users import ROLE_ACCOUNTANT

    r = client.post(
        "/api/users/invite",
        json={"username": "cpa", "role": ROLE_ACCOUNTANT},
        headers={"host": "127.0.0.1:3001"},
    )
    assert r.status_code == 201, r.text
    address = r.json()["address"]
    assert not re.search(r"://(127\.0\.0\.1|localhost)(:|$)", address), address
    assert address.endswith(":3001")
    r = client.post(
        "/api/users/invite",
        json={"username": "cpa2", "role": ROLE_ACCOUNTANT},
        headers={"host": "books.example.com"},
    )
    assert r.json()["address"] == "http://books.example.com"


# ---------------------------------------------------------------------------
# NEW-152 / W-19 / NEW-153: the exceptions report on the person's clock
# ---------------------------------------------------------------------------


def test_the_period_and_the_times_follow_the_pages_clock(
    client, db_session, seed_accounts
):
    from urllib.parse import quote

    from app.models.audit import AuditLog
    from app.services.closing_date_exceptions import local_words, utc_bounds, zone_words

    start, end = utc_bounds(date(2026, 10, 19), date(2026, 10, 19), 300)
    assert start == datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)
    assert (
        local_words(datetime(2026, 10, 20, 0, 12, tzinfo=timezone.utc), 300)
        == "2026-10-19 19:12"
    )
    assert zone_words(300) == "UTC−05:00" and zone_words(-60) == "UTC+01:00"

    accounts = seed_accounts
    _ok = lambda r: (r.status_code in (200, 201), r.text)[0] and r.json()  # noqa: E731
    _ok(
        client.put(
            "/api/settings",
            json={"closing_date": "2026-02-28", "closing_date_password": "shut-tight"},
        )
    )
    r = client.post(
        "/api/journal",
        json={
            "date": "2026-01-15",
            "description": "Back-dated",
            "lines": [
                {"account_id": accounts["6300"].id, "debit": "30.00", "credit": "0"},
                {"account_id": accounts["1000"].id, "debit": "0", "credit": "30.00"},
            ],
        },
        headers={"X-Closing-Date-Password": quote("shut-tight")},
    )
    assert r.status_code == 201, r.text
    # the override's rows were stamped now (UTC); pretend they were made at
    # 7:12 PM Central on Oct 19, which is 00:12 UTC on Oct 20
    stamp = datetime(2026, 10, 20, 0, 12, tzinfo=timezone.utc)
    for row in db_session.query(AuditLog).all():
        row.timestamp = stamp
    db_session.commit()
    central = "&tz_offset=300"
    # on the person's clock the override is Oct 19's
    data = client.get(
        f"/api/reports/closing-date-exceptions?start_date=2026-10-01&end_date=2026-10-19{central}"
    ).json()
    assert data["count"] == 1 and data["timezone"] == "UTC−05:00"
    assert data["events"][0]["when_local"] == "2026-10-19 19:12"
    # in UTC it is Oct 20's, and not in a period ending on the 19th
    data = client.get(
        "/api/reports/closing-date-exceptions?start_date=2026-10-01&end_date=2026-10-19"
    ).json()
    assert data["count"] == 0
    data = client.get(
        "/api/reports/closing-date-exceptions?start_date=2026-10-20&end_date=2026-10-20"
    ).json()
    assert data["count"] == 1
    # the lines' accounts by name, never by id
    data = client.get(
        f"/api/reports/closing-date-exceptions?start_date=2026-10-19&end_date=2026-10-19{central}"
    ).json()
    fields = [f for ch in data["events"][0]["changes"] for f in ch["fields"]]
    names = {f["field"]: f["after"] for f in fields}
    assert "account_id" not in names
    assert any(
        f["field"] == "account" and "Insurance" in f["after"] for f in fields
    ), fields
    csv_text = client.get(
        f"/api/reports/closing-date-exceptions/csv?start_date=2026-10-19&end_date=2026-10-19{central}"
    ).text
    assert (
        "2026-10-19 19:12" in csv_text
        and "UTC−05:00" in csv_text
        and "account_id" not in csv_text
    )
    pdf = client.get(
        f"/api/reports/closing-date-exceptions/pdf?start_date=2026-10-19&end_date=2026-10-19{central}"
    )
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_the_pdfs_summary_says_one_override():
    from app.services.closing_date_exceptions import exceptions_section

    section = exceptions_section(
        {
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "closing_date": "",
            "timezone": "UTC−05:00",
            "events": [
                {
                    "when": "",
                    "when_local": "2026-10-19 19:12",
                    "who": "admin",
                    "closing_date": "2026-02-28",
                    "transaction_date": "2026-01-15",
                    "changes": [],
                }
            ],
            "count": 1,
            "changes": 0,
        }
    )
    last = section["rows"][-1]["cells"][0]
    assert last == "1 override, 0 changes"
    assert "times in UTC−05:00" in section["period"]


# ---------------------------------------------------------------------------
# NEW-154: the package's audit range
# ---------------------------------------------------------------------------


def test_the_packages_audit_file_stays_inside_the_period(
    client, db_session, seed_accounts
):
    assert (
        client.put(
            "/api/settings",
            json={"closing_date": "2026-02-28", "closing_date_password": "x-y-z"},
        ).status_code
        == 200
    )
    m = client.get(
        "/api/reports/accountants-package/manifest?start_date=2025-01-01&end_date=2025-12-31"
    ).json()
    audit = next(f for f in m["files"] if f.startswith("audit-log_"))
    assert audit == "audit-log_2025-01-01_2025-12-31.csv"
    m = client.get(
        "/api/reports/accountants-package/manifest?start_date=2026-01-01&end_date=2026-12-31"
    ).json()
    audit = next(f for f in m["files"] if f.startswith("audit-log_"))
    assert audit == "audit-log_2026-02-28_2026-12-31.csv"


# ---------------------------------------------------------------------------
# NEW-155, NEW-160, NEW-161
# ---------------------------------------------------------------------------


def test_the_passphrase_has_no_look_alikes():
    from app.services.accountants_copy import _passphrase

    for _ in range(50):
        p = _passphrase()
        assert len(p) == 24 and not set(p) & set("0O1lI"), p


def test_a_recovery_key_typed_with_digits_for_letters_reads():
    key = "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567"
    typed = (
        key.replace("O", "0")
        .replace("I", "1")
        .replace("B", "8")
        .lower()
        .replace("-", " ")
    )
    assert company_keyring.normalize_recovery_key(
        typed
    ) == company_keyring.normalize_recovery_key(key)
    assert company_keyring.key_from_recovery(
        typed
    ) == company_keyring.key_from_recovery(key)


def test_the_key_file_is_dated_on_the_persons_clock():
    text = company_keyring.recovery_file_text("Harbor Light", "ABCD-EFGH")
    assert f"Made {datetime.now():%B %d, %Y}." in text


# ---------------------------------------------------------------------------
# NEW-158: which backups are plain
# ---------------------------------------------------------------------------


def test_the_backups_list_says_which_are_plain(tmp_path, monkeypatch):
    from app.services import backup_service as bs

    monkeypatch.setattr(bs, "BACKUP_DIR", tmp_path)
    plain = tmp_path / "books_20260101_000000.db"
    conn = company_file.connect_raw(plain, None)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    enc = tmp_path / "books_20260102_000000.db"
    conn = company_file.connect_raw(enc, bytes(range(32)))
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    flags = {b["filename"]: b["encrypted"] for b in bs.list_backup_files()}
    assert flags[plain.name] is False and flags[enc.name] is True


# ---------------------------------------------------------------------------
# The accountant and the employee directory; Turn off's password
# ---------------------------------------------------------------------------


def test_the_accountant_is_refused_the_employee_directory(client, db_session):
    from app.models.users import ROLE_ACCOUNTANT, User
    from app.services import auth as auth_service

    db_session.add(
        User(
            username="cpa",
            display_name="CPA",
            password_hash=auth_service.hash_password("cpa-own-password-1"),
            role=ROLE_ACCOUNTANT,
            is_active=True,
        )
    )
    db_session.commit()
    client.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "cpa", "password": "cpa-own-password-1"},
        ).status_code
        == 200
    )
    assert client.get("/api/employees").status_code == 403
    assert client.get("/api/customers").status_code == 200


def test_turning_off_asks_the_administrators_password():
    src = _src("app/routes/company_file.py")
    assert 'if not data.enabled and request.session.get("user_id"):' in src
    js = _src("app/static/js/settings.js")
    assert "const asksPassword = desktopKey || (!enable && App.username);" in js


# ---------------------------------------------------------------------------
# The pages (source pins; the browser run covers the behaviour)
# ---------------------------------------------------------------------------


def test_the_pages_carry_the_round_two_fixes():
    app = _src("app/static/js/app.js")
    # W-20: the copy is locked but for the journal
    assert "isAccountant() { return App.role === 'accountant' || App.inCopy; }" in app
    assert "App.inCopy = !!a.accountants_copy;" in app
    # W-22: the sidebar over an addressed dialog
    assert "document.body.classList.toggle('dialog-addressed', !!clear);" in app
    assert "body.dialog-addressed #sidebar" in _src("app/static/css/style.css")
    # NEW-164: the list is one sign-in's and one company's, numbered per load
    assert "_tabsOwnerChanged(status)" in app and "load: App._loadId" in app
    # a report view names itself even before its dialog is up
    assert "ReportsPage._VIEWS[view[1]]" in app
    # documents opened from their lists carry their address
    for rel, route in (
        ("invoices", "invoices"),
        ("bills", "bills"),
        ("payments", "payments"),
        ("deposits", "deposits"),
        ("expenses", "expenses"),
        ("credit_memos", "credit-memos"),
        ("vendor_credits", "vendor-credits"),
    ):
        assert (
            f"if (App.documentAddress) App.documentAddress(`#/{route}/${{Number(id)}}`);"
            in _src(f"app/static/js/{rel}.js")
        ), rel
    # W-21: the toast counts register entries
    assert "(result.posted || 0)" in _src("app/static/js/iif.js")
    # NEW-163: the document named once; the move controls are writes
    reports = _src("app/static/js/reports.js")
    assert 'id="rc-move" data-accountant-ok data-write' in reports
    assert 'class="rc-tick" data-write' in reports
    assert (
        "const what = l.document ? escapeHtml(l.document) : `${escapeHtml(l.source_label)}"
        in reports
    )
    # NEW-152: the page's clock rides on the request
    assert (
        "closing-date-exceptions?${qs}" in reports
        and "tz_offset=${new Date().getTimezoneOffset()}" in reports
    )
    # NEW-151: no end date reads "none"
    settings = _src("app/static/js/settings.js")
    assert (
        "askEndDate(" in settings
        and "Leave it empty for a sign-in with no end date." in settings
    )
    # NEW-158: the plain backups counted
    assert "Backups made while it is on are encrypted" in settings
    # the recovery key through a Save panel on the desktop
    assert "api.save_secret_file" in settings and "api.save_secret_file" in _src(
        "app/static/js/auth.js"
    )
    launcher = _src("desktop_launcher.py")
    assert "def save_secret_file(self, name: str, text: str) -> dict:" in launcher
    assert "webview.SAVE_DIALOG" in launcher


def test_the_iif_export_writes_the_transfer_arrow_as_ascii():
    from app.services.iif_export import to_ansi

    assert to_ansi("Savings → Checking") == b"Savings -> Checking"
