"""The update check skips a release this Mac can't open (2.26.0).

2.26.0's Mac app is built on macOS 15 runners, so its bundled Homebrew
libraries need macOS 15. latest.json names that floor (`macos_minimum`), and
a Mac below it is not offered an update it could not open.
"""

import httpx
import pytest

from app.routes import system as system_routes

MANIFEST = {
    "version": "99.0.0",
    "macos_minimum": "15.0",
    "download_url": "https://www.slowbookspro.com/#install",
}


@pytest.fixture(autouse=True)
def _desktop(monkeypatch):
    system_routes._cache["at"] = 0.0
    system_routes._cache["result"] = None
    monkeypatch.setenv("SLOWBOOKS_DESKTOP", "1")
    monkeypatch.setenv("SLOWBOOKS_UPDATE_CHECK", "1")

    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, json=MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    yield
    system_routes._cache["at"] = 0.0
    system_routes._cache["result"] = None


def _on(monkeypatch, platform_name, mac_version=""):
    monkeypatch.setattr(system_routes.sys, "platform", platform_name)
    monkeypatch.setattr(
        system_routes.platform, "mac_ver", lambda: (mac_version, ("", "", ""), "")
    )


def test_a_mac_below_the_minimum_is_not_offered_it(client, monkeypatch):
    _on(monkeypatch, "darwin", "14.7.2")
    assert client.get("/api/system/update-check").json() == {"update_available": False}


@pytest.mark.parametrize("version", ["15.0", "15.6.1", "26.4.1"])
def test_a_mac_at_or_above_the_minimum_is_offered_it(client, monkeypatch, version):
    _on(monkeypatch, "darwin", version)
    body = client.get("/api/system/update-check").json()
    assert body["update_available"] is True
    assert body["latest_version"] == "99.0.0"


@pytest.mark.parametrize("platform_name", ["win32", "linux"])
def test_windows_and_linux_ignore_the_mac_minimum(client, monkeypatch, platform_name):
    _on(monkeypatch, platform_name)
    assert client.get("/api/system/update-check").json()["update_available"] is True


def test_no_minimum_named_offers_it_everywhere():
    assert system_routes.runs_here({"version": "99.0.0"}) is True
