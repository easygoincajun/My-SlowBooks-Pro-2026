"""Restore from a backup file chosen on the person's computer.

The file is copied into the backup folder and restored the ordinary way, so
it gets the same checks as a listed backup: another company's books need a
second confirmation, and a safety copy of the books as they are comes first.
Anything that isn't a company backup is refused and nothing is left behind.
"""

import io
import sqlite3
from contextlib import closing

from app.services import backup_service
from tests.test_backups_per_company import (  # noqa: F401 (fixture)
    _change,
    _make_books,
    _value,
    books,
)


def _head():
    return backup_service._migration_script()[1].get_current_head()


def _upload(client, name, data):
    return client.post(
        "/api/backups/upload",
        files={"file": (name, io.BytesIO(data), "application/octet-stream")},
    )


def _company_file(tmp_path, company, value):
    path = tmp_path / "from-a-thumb-drive.db"
    _make_books(path, company, value, revision=_head())
    return path.read_bytes()


def test_a_backup_from_elsewhere_is_restored(books, client, tmp_path):  # noqa: F811
    data = _company_file(tmp_path, "Harbor Light Bakery", "from the drive")
    r = _upload(client, "harbor-light-bakery_20260101_120000.db", data)
    assert r.status_code == 200, r.text
    name = r.json()["filename"]
    assert name == "harbor-light-bakery_20260101_120000.db"
    assert (books.backups / name).read_bytes() == data

    r = client.post("/api/backups/restore", json={"filename": name})
    assert r.status_code == 200, r.text
    assert _value(books.live) == "from the drive"
    assert _value(books.backups / r.json()["safety_backup"]) == "original"


def test_another_companys_file_still_needs_a_second_confirmation(
    books, client, tmp_path  # noqa: F811
):
    data = _company_file(tmp_path, "Riverbend Books", "theirs")
    name = _upload(client, "riverbend.db", data).json()["filename"]
    r = client.post("/api/backups/restore", json={"filename": name})
    assert r.status_code == 409
    assert _value(books.live) == "original"


def test_a_file_that_is_not_a_backup_is_refused(books, client, tmp_path):  # noqa: F811
    plain = tmp_path / "plain.db"
    with closing(sqlite3.connect(plain)) as conn:
        conn.execute("CREATE TABLE notes (v TEXT)")
        conn.commit()
    for name, data in (
        ("photo.db", b"\x89PNG\r\n\x1a\n" + b"0" * 100),
        ("plain.db", plain.read_bytes()),  # a database, not a company file
    ):
        r = _upload(client, name, data)
        assert r.status_code == 400, r.text
    assert [p.name for p in books.backups.iterdir()] == []


def test_a_name_never_overwrites_or_escapes_the_folder(
    books, client, tmp_path  # noqa: F811
):
    data = _company_file(tmp_path, "Harbor Light Bakery", "v1")
    first = _upload(client, "same.db", data).json()["filename"]
    second = _upload(client, "same.db", data).json()["filename"]
    assert (first, second) == ("same.db", "same-2.db")

    r = _upload(client, "../../outside.db", data)
    assert r.status_code == 200, r.text
    assert (books.backups / r.json()["filename"]).exists()
    assert not (tmp_path / "outside.db").exists()
