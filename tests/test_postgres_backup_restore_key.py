"""A Postgres backup is restored without a recovery key (Discussion #417).

backup_key_problem() took any backup that wasn't plain SQLite for an
encrypted company file. A Postgres backup is pg_dump's custom format, saved
as .sql, so every restore on a Docker or server install was refused with
"made under another key" and asked for a recovery key it never had, and the
backup lists marked those backups encrypted.
"""

from app.services import backup_service

PG_DUMP_HEAD = b"PGDMP\x01\x0e\x00\x04\x08\x01\x01\x01" + b"\x00" * 64


def _write(tmp_path, monkeypatch, name, data):
    monkeypatch.setattr(backup_service, "BACKUP_DIR", tmp_path)
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_a_postgres_backup_needs_no_key(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch, "Acme_20261022_120000.sql", PG_DUMP_HEAD)
    assert backup_service.is_encrypted_backup(path) is False
    assert backup_service.backup_key_problem(path.name) is None


def test_a_postgres_backup_is_not_listed_as_encrypted(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "Acme_20261022_120000.sql", PG_DUMP_HEAD)
    listed = backup_service.list_backup_files()
    assert [f["encrypted"] for f in listed] == [False]


def test_an_encrypted_sqlite_backup_still_counts(tmp_path, monkeypatch):
    # not plain SQLite and named .db: an encrypted company file, as before
    path = _write(tmp_path, monkeypatch, "Acme_20261022_120000.db", b"\x8f" * 4096)
    assert backup_service.is_encrypted_backup(path) is True


def test_a_plain_sqlite_backup_is_not_encrypted(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        monkeypatch,
        "Acme_20261022_120000.db",
        b"SQLite format 3\x00" + b"\x00" * 100,
    )
    assert backup_service.is_encrypted_backup(path) is False
    assert backup_service.backup_key_problem(path.name) is None
