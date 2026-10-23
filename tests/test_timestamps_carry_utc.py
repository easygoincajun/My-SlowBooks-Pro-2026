"""Users' "Last login" and API tokens' "Last used" showed tomorrow's date
from 7 pm Central (2.23.2 gate, NEW-110): SQLite stores naive UTC, the
routes sent it bare, and the browser read it as local time. They carry
their zone now, as the backups list's stamps do."""

from datetime import datetime, timezone

from app.routes import api_tokens, users


def test_a_naive_stamp_is_sent_as_utc():
    naive = datetime(2026, 10, 15, 0, 30, 0)
    assert users._utc_iso(naive) == "2026-10-15T00:30:00+00:00"
    assert api_tokens._utc_iso(naive) == "2026-10-15T00:30:00+00:00"
    aware = datetime(2026, 10, 14, 19, 30, 0, tzinfo=timezone.utc)
    assert users._utc_iso(aware) == "2026-10-14T19:30:00+00:00"
    assert users._utc_iso(None) is None


def test_the_users_list_sends_its_stamps_with_a_zone(client):
    resp = client.get("/api/users")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    rows = rows if isinstance(rows, list) else rows.get("users", rows.get("items", []))
    assert rows, "the signed-in administrator is a user"
    for row in rows:
        for field in ("created_at", "last_login_at"):
            if row.get(field):
                assert row[field].endswith("+00:00"), (field, row[field])
