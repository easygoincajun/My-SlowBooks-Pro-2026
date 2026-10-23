"""/api/system + /api/system/update-check (desktop update badge).

The update check is best-effort by contract: anything short of a valid
manifest advertising a strictly newer version must come back as
{"update_available": False} — never an error status the frontend has to
handle.
"""

import httpx
import pytest

from app import __version__
from app.routes import system as system_routes


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The module caches one update-check result per process — reset it so
    tests don't see each other's answers."""
    system_routes._cache["at"] = 0.0
    system_routes._cache["result"] = None
    yield
    system_routes._cache["at"] = 0.0
    system_routes._cache["result"] = None


def test_system_info_reports_version(client):
    r = client.get("/api/system")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == __version__
    assert body["desktop"] is False
    assert body["update_check_enabled"] is False


def test_system_info_desktop_flag(client, monkeypatch):
    # A desktop install hears about new versions unless its owner turns the
    # check off: an install that never asks never learns of the next release.
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.delenv("SLOWBOOKS_UPDATE_CHECK", raising=False)
    assert client.get("/api/system").json()["desktop"] is True
    assert client.get("/api/system").json()["update_check_enabled"] is True


@pytest.mark.parametrize("value", ["0", "false", "off", " No "])
def test_system_info_reports_the_update_check_turned_off(client, monkeypatch, value):
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.setenv("SLOWBOOKS_UPDATE_CHECK", value)
    assert client.get("/api/system").json()["update_check_enabled"] is False


def test_update_check_turned_off_makes_no_request(client, monkeypatch):
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.setenv("SLOWBOOKS_UPDATE_CHECK", "0")

    async def unexpected_get(*args, **kwargs):
        pytest.fail("an update check turned off made an outbound request")

    monkeypatch.setattr(httpx.AsyncClient, "get", unexpected_get)
    assert client.get("/api/system/update-check").json() == {"update_available": False}


def test_update_check_noop_outside_desktop_mode(client, monkeypatch):
    monkeypatch.delenv("SLOWBOOKS_DESKTOP", raising=False)
    r = client.get("/api/system/update-check")
    assert r.status_code == 200
    assert r.json() == {"update_available": False}


def _mock_manifest(monkeypatch, payload=None, exc=None):
    """Replace httpx.AsyncClient.get with a canned latest.json response."""

    async def fake_get(self, url, **kwargs):
        if exc is not None:
            raise exc
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def test_update_check_reports_newer_version(client, monkeypatch):
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.setenv("SLOWBOOKS_UPDATE_CHECK", "1")
    _mock_manifest(
        monkeypatch,
        payload={
            "version": "99.0.0",
            "download_url": "https://www.slowbookspro.com/#install",
            "notes_url": "https://example.com/notes",
        },
    )
    body = client.get("/api/system/update-check").json()
    assert body["update_available"] is True
    assert body["latest_version"] == "99.0.0"
    assert body["download_url"] == "https://www.slowbookspro.com/#install"


def test_update_check_same_version_is_not_an_update(client, monkeypatch):
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.setenv("SLOWBOOKS_UPDATE_CHECK", "1")
    _mock_manifest(
        monkeypatch,
        payload={"version": __version__, "download_url": "https://x.example"},
    )
    assert client.get("/api/system/update-check").json() == {"update_available": False}


def test_update_check_swallows_network_errors(client, monkeypatch):
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.setenv("SLOWBOOKS_UPDATE_CHECK", "1")
    _mock_manifest(monkeypatch, exc=httpx.ConnectError("offline"))
    r = client.get("/api/system/update-check")
    assert r.status_code == 200
    assert r.json() == {"update_available": False}


def test_update_check_result_is_cached(client, monkeypatch):
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.setenv("SLOWBOOKS_UPDATE_CHECK", "1")
    calls = {"n": 0}

    async def counting_get(self, url, **kwargs):
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"version": "99.0.0", "download_url": "https://x.example"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", counting_get)
    first = client.get("/api/system/update-check").json()
    second = client.get("/api/system/update-check").json()
    assert first == second
    assert calls["n"] == 1


def test_version_comparison():
    assert system_routes.is_newer("2.1.1", "2.1.0")
    assert system_routes.is_newer("v2.2.0", "2.1.9")
    assert not system_routes.is_newer("2.1.0", "2.1.0")
    assert not system_routes.is_newer("2.0.9", "2.1.0")
    assert not system_routes.is_newer("", "2.1.0")


# This fork's builds read its own GitHub releases, never the upstream
# manifest: the upstream installer installs over the fork (same AppId).


def test_the_check_asks_this_forks_releases():
    assert system_routes.LATEST_URL == (
        "https://api.github.com/repos/easygoincajun/My-SlowBooks-Pro-2026/releases/latest"
    )


def test_a_github_release_offers_its_installer(client, monkeypatch):
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.setenv("SLOWBOOKS_UPDATE_CHECK", "1")
    page = (
        "https://github.com/easygoincajun/My-SlowBooks-Pro-2026/releases/tag/v99.0.0.1"
    )
    exe = "https://github.com/easygoincajun/My-SlowBooks-Pro-2026/releases/download/v99.0.0.1/SlowBooksPro-Setup.exe"
    _mock_manifest(
        monkeypatch,
        payload={
            "tag_name": "v99.0.0.1",
            "html_url": page,
            "assets": [
                {"name": "SHA256SUMS.txt", "browser_download_url": page + "/sums"},
                {"name": "SlowBooksPro-Setup.exe", "browser_download_url": exe},
            ],
        },
    )
    body = client.get("/api/system/update-check").json()
    assert body == {
        "update_available": True,
        "latest_version": "99.0.0.1",
        "download_url": exe,
        "notes_url": page,
    }


def test_a_release_without_an_installer_offers_its_page():
    assert (
        system_routes.manifest(
            {
                "tag_name": "v2.27.0.1",
                "html_url": "https://example.test/r",
                "assets": [],
            }
        )["download_url"]
        == "https://example.test/r"
    )


def test_the_upstream_release_a_fork_build_carries_is_not_newer():
    # 2.26.0.1 carries upstream 2.26.0: that release is no update for it
    assert not system_routes.is_newer("2.26.0", "2.26.0.1")
    assert system_routes.is_newer("2.26.0.2", "2.26.0.1")
    assert system_routes.is_newer("2.27.0.1", "2.26.0.1")
