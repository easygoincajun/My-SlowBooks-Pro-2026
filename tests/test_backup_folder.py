"""The backup folder is chosen in Settings.

New backups go to the chosen folder; backups already in the default folder
stay listed, downloadable and restorable. A folder that isn't there (a drive
that isn't plugged in) is refused when chosen and makes a backup fail with a
sentence, never a folder quietly created on the startup disk.
"""

from tests.test_backups_per_company import (  # noqa: F401 (fixture)
    _backup,
    _change,
    _listed,
    _value,
    books,
)


def test_the_default_folder_until_one_is_chosen(books, client):
    r = client.get("/api/backups/folder")
    assert r.status_code == 200, r.text
    assert r.json()["is_default"] is True
    assert r.json()["folder"] == str(books.backups)


def test_new_backups_go_to_the_chosen_folder(books, client, tmp_path):
    old = _backup(client)
    chosen = tmp_path / "external"
    chosen.mkdir()
    r = client.put("/api/backups/folder", json={"folder": str(chosen)})
    assert r.status_code == 200, r.text
    assert r.json()["is_default"] is False

    new = _backup(client)
    assert (chosen / new).exists() and not (books.backups / new).exists()
    # the one made before the change is still there to restore
    assert set(_listed(client)) == {old, new}
    assert client.get(f"/api/backups/download/{new}").status_code == 200
    assert client.get(f"/api/backups/download/{old}").status_code == 200

    _change(books.live, "changed since")
    r = client.post("/api/backups/restore", json={"filename": old})
    assert r.status_code == 200, r.text
    assert _value(books.live) == "original"
    assert (chosen / r.json()["safety_backup"]).exists()


def test_a_folder_that_cannot_be_used_is_refused(books, client, tmp_path):
    for folder in (str(tmp_path / "missing"), "relative/path"):
        r = client.put("/api/backups/folder", json={"folder": folder})
        assert r.status_code == 400, r.text
    assert client.get("/api/backups/folder").json()["is_default"] is True


def test_an_unplugged_folder_fails_the_backup_and_creates_nothing(
    books, client, tmp_path
):
    chosen = tmp_path / "drive"
    chosen.mkdir()
    client.put("/api/backups/folder", json={"folder": str(chosen)})
    chosen.rmdir()

    assert client.get("/api/backups/folder").json()["available"] is False
    r = client.post("/api/backups")
    assert r.status_code == 500
    assert "not available" in r.json()["detail"]
    assert not chosen.exists()


def test_blank_goes_back_to_the_default(books, client, tmp_path):
    chosen = tmp_path / "external"
    chosen.mkdir()
    client.put("/api/backups/folder", json={"folder": str(chosen)})
    r = client.put("/api/backups/folder", json={"folder": ""})
    assert r.status_code == 200, r.text
    assert r.json()["is_default"] is True
    assert (books.backups / _backup(client)).exists()
