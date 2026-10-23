"""Server processes started together share one session key (2.26.0 gate).

The Docker image runs two uvicorn workers. On a first start neither found
.slowbooks-session.key, each wrote its own and kept it, and a cookie signed
by one worker was refused by the other: on a fresh 2.26.0 stack 12 of 30
requests in one signed-in session answered 401. The first process to
create the file now wins and the others read its key. The race is decided
in microseconds, so the tests stage the other worker's write directly.
"""

import os

from app.services import auth

OTHER = "the-other-workers-key-0123456789"


def _another_worker_writes_first(key_path):
    """A stand-in for the worker that starts a moment sooner: it creates the
    key file after this one looked and found none, before this one writes."""
    key_path.write_text(OTHER + "\n", encoding="utf-8")


def test_a_worker_that_loses_the_race_uses_the_winners_key(tmp_path, monkeypatch):
    key_path = tmp_path / ".slowbooks-session.key"
    real_link = os.link

    def link(src, dst, *a, **kw):
        _another_worker_writes_first(key_path)
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(auth.os, "link", link)
    assert auth._session_secret_from_file(key_path) == OTHER
    assert key_path.read_text(encoding="utf-8").strip() == OTHER
    assert [p.name for p in tmp_path.iterdir()] == [".slowbooks-session.key"]


def test_without_hard_links_the_exclusive_create_decides(tmp_path, monkeypatch):
    key_path = tmp_path / ".slowbooks-session.key"

    def no_links(src, dst, *a, **kw):
        _another_worker_writes_first(key_path)
        raise PermissionError("hard links not supported here")

    monkeypatch.setattr(auth.os, "link", no_links)
    assert auth._session_secret_from_file(key_path) == OTHER
    assert [p.name for p in tmp_path.iterdir()] == [".slowbooks-session.key"]


def test_without_hard_links_a_first_start_still_writes_its_key(tmp_path, monkeypatch):
    key_path = tmp_path / ".slowbooks-session.key"

    def no_links(src, dst, *a, **kw):
        raise PermissionError("hard links not supported here")

    monkeypatch.setattr(auth.os, "link", no_links)
    key = auth._session_secret_from_file(key_path)
    assert key and key_path.read_text(encoding="utf-8").strip() == key


def test_an_existing_key_is_kept(tmp_path):
    key_path = tmp_path / ".slowbooks-session.key"
    key_path.write_text("kept-key-0123456789\n", encoding="utf-8")
    assert auth._session_secret_from_file(key_path) == "kept-key-0123456789"


def test_a_first_start_writes_the_key_it_returns(tmp_path):
    key_path = tmp_path / ".slowbooks-session.key"
    key = auth._session_secret_from_file(key_path)
    assert key and key_path.read_text(encoding="utf-8").strip() == key
    assert auth._session_secret_from_file(key_path) == key
