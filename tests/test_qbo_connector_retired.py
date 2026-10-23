"""The QuickBooks Online connector is gone (2.23.2).

Intuit OAuth, the import and the export over Intuit's API needed each
user's own app in Intuit's developer portal, and keys that reach a real
company sit behind an app questionnaire a bookkeeper cannot answer, so the
connector never served an end user while the product touted it. It is
retired: no page, no Settings section, no routes, no SDK, and a company
file that still holds an old connection's credentials is cleaned on the
first start after upgrading. QuickBooks Online is migrated from its own
Export data zip (services/qbo_export_import.py) instead.

What an earlier import left behind stays readable: the mappings table, and
the void and edit paths of documents such an import created
(services/qbo_documents.py), so a company connected back then keeps
working.
"""

import re
from pathlib import Path

from app.models.settings import DEFAULT_SETTINGS, Settings
from app.services.settings_service import (
    ENCRYPTED_SETTINGS_KEYS,
    RETIRED_SETTINGS_KEYS,
    remove_retired_settings,
)

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_api_has_no_connector_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert [p for p in paths if p.startswith("/api/qbo")] == []
    assert client.get("/api/qbo/status").status_code == 404
    assert client.post("/api/qbo/import-runs").status_code == 404
    # the Export data route is what replaces it
    assert "/api/migration/{source}/dry-run" in paths
    sources = {s["key"] for s in client.get("/api/migration/sources").json()}
    assert "qbo_export" in sources


def test_the_shell_settings_and_tree_have_no_connector():
    app_js = _read("app/static/js/app.js")
    assert "'/qbo'" not in app_js and "QBOPage" not in app_js
    settings_js = _read("app/static/js/settings.js")
    assert "qbo_" not in settings_js and "developer.intuit.com" not in settings_js
    assert not (ROOT / "app/static/js/qbo.js").exists()
    assert not (ROOT / "app/routes/qbo.py").exists()
    assert not (ROOT / "app/services/qbo_service.py").exists()
    assert not (ROOT / "docs/setup-qbo.md").exists()
    for rel in ("README.md", "docs/features.md", "docs/migrate-from-quickbooks.md"):
        assert "setup-qbo" not in _read(rel), rel
    requirements = _read("requirements.txt")
    assert not re.search(r"(?m)^(python-quickbooks|intuit-oauth)", requirements)
    main = _read("app/main.py")
    assert "/api/qbo" not in main and "routes import qbo" not in main


def test_the_shell_links_no_connector_and_loads_no_missing_script():
    """index.html, the SPA's shell, is outside app/ and kept the connector's
    sidebar link ("Page not found" on a click) and its script tag (a 404 on
    every page load) after the rest was removed (round 3, W-5 / NEW-115).
    No link to #/qbo, no qbo.js, and every script the shell loads exists."""
    shell = _read("index.html")
    assert "#/qbo" not in shell and "qbo.js" not in shell
    assert "QuickBooks Online" not in shell
    scripts = re.findall(r'<script src="/static/js/([^"]+)"', shell)
    assert len(scripts) > 20
    missing = [name for name in scripts if not (ROOT / "app/static/js" / name).exists()]
    assert missing == [], missing
    links = re.findall(r'href="#/([a-z-]+)"', shell)
    assert "iif" in links and "migrate" in links and "qbo" not in links


def test_the_pages_name_an_imported_journals_source(client):
    """A posting's Type on the registers, the General Ledger and a journal
    entry's view goes through sourceWord, which names each migration's
    journals (NEW-114: the register read "qbo export import")."""
    utils = _read("app/static/js/utils.js")
    for key, word in (
        ("qbo_export_import", "QuickBooks Online export"),
        ("wave_import", "Wave import"),
        ("gnucash_import", "GnuCash import"),
    ):
        assert f"{key}: '{word}'" in utils, key
    for rel in (
        "app/static/js/banking.js",
        "app/static/js/reports.js",
        "app/static/js/journal.js",
    ):
        src = _read(rel)
        assert "source_type || '').replace(/_/g, ' ')" not in src, rel
        assert (
            "escapeHtml(e.source_type)}" not in src
            and "escapeHtml(entry.source_type)}" not in src
        ), rel
        assert "sourceWord(" in src, rel


def test_no_connector_settings_remain_in_the_defaults():
    assert [k for k in DEFAULT_SETTINGS if k.startswith("qbo_")] == []
    assert [k for k in ENCRYPTED_SETTINGS_KEYS if k.startswith("qbo_")] == []
    assert set(RETIRED_SETTINGS_KEYS) >= {
        "qbo_client_id",
        "qbo_client_secret",
        "qbo_access_token",
        "qbo_refresh_token",
        "qbo_realm_id",
    }


def test_an_old_connections_credentials_are_removed_on_start(db_session):
    """A company file connected while the connector existed holds an Intuit
    app's credentials and the connection's tokens. Nothing reads them now,
    and the startup pass deletes the rows; the company's other settings
    are untouched."""
    db_session.add_all(
        [
            Settings(key="qbo_client_id", value="ABo8gw"),
            Settings(key="qbo_access_token", value="enc:tok"),
            Settings(key="qbo_realm_id", value="4620816365"),
            Settings(key="company_name", value="Harbor Light Bakery"),
        ]
    )
    db_session.commit()
    assert remove_retired_settings(db_session) == 3
    keys = {row.key for row in db_session.query(Settings).all()}
    assert not [k for k in keys if k.startswith("qbo_")]
    assert "company_name" in keys
    assert remove_retired_settings(db_session) == 0


def test_the_sidebar_and_write_actions_name_no_connector_page():
    app_js = _read("app/static/js/app.js")
    assert (
        "QuickBooks Online" not in app_js.split("WRITE_ACTIONS")[0].split("routes")[-1]
    )
    assert "QBOPage." not in app_js


def test_what_an_earlier_import_left_behind_still_reads(db_session):
    """The documents an import created keep their void and edit paths."""
    from app.models.qbo_mapping import QBOMapping
    from app.services import qbo_common, qbo_documents

    assert QBOMapping.__tablename__ == "qbo_mappings"
    assert qbo_documents.invoice_origin.__doc__
    assert qbo_common.discount_item_ids(db_session) == set()
