"""SimpleFIN bank feeds: token claim, request builders, sync dedup,
bank-rule parity with OFX, settings redaction, and route error paths."""

import base64
import json
from datetime import date
from decimal import Decimal

import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app

from app.models.accounts import Account, AccountType
from app.models.bank_rules import BankRule
from app.models.banking import BankAccount, BankTransaction
from app.services import simplefin_service as sf
from app.services.settings_service import get_setting_raw, set_setting

CLAIM_URL = "https://bridge.example.com/simplefin/claim/DEMO"
ACCESS_URL = "https://user123:pass456@bridge.example.com/simplefin"

SF_DATA = {
    "errors": [],
    "accounts": [
        {
            "id": "ACT-1",
            "name": "Demo Checking",
            "org": {"name": "Demo Bank"},
            "currency": "USD",
            "balance": "1234.56",
            "transactions": [
                {
                    "id": "TXN-1",
                    "posted": 1786320000,  # 2026-08-10 UTC
                    "amount": "-55.50",
                    "description": "Fishing bait",
                    "payee": "Johns Fishin Shack",
                },
                {
                    "id": "TXN-2",
                    "posted": 1786233600,
                    "amount": "2500.00",
                    "description": "Payroll",
                    "payee": "ACME LLC",
                },
                {
                    "id": "TXN-PENDING",
                    "posted": 1786320000,
                    "amount": "-9.99",
                    "description": "Pending card hold",
                    "pending": True,
                },
            ],
        }
    ],
}


def _token(url=CLAIM_URL):
    return base64.b64encode(url.encode()).decode()


def _resp(status=200, text="", json_body=None):
    if json_body is not None:
        return httpx.Response(status, json=json_body)
    return httpx.Response(status, text=text)


def _mk_bank_account(db_session, name="Feed Checking"):
    ba = BankAccount(name=name, bank_name="Demo Bank")
    db_session.add(ba)
    db_session.commit()
    return ba


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_decode_setup_token_roundtrip():
    assert sf.decode_setup_token(_token()) == CLAIM_URL


def test_decode_setup_token_rejects_http():
    with pytest.raises(sf.SimpleFINError):
        sf.decode_setup_token(_token("http://insecure.example.com/claim"))


def test_decode_setup_token_rejects_garbage():
    with pytest.raises(sf.SimpleFINError):
        sf.decode_setup_token("not-base64!!!")
    with pytest.raises(sf.SimpleFINError):
        sf.decode_setup_token("")


def test_build_accounts_request_splits_credentials():
    req = sf.build_accounts_request(ACCESS_URL, start_date=date(2026, 8, 1))
    assert req["url"] == "https://bridge.example.com/simplefin/accounts"
    assert req["auth"] == ("user123", "pass456")
    assert req["params"]["start-date"] == 1785542400  # 2026-08-01T00:00Z
    # credentials must not leak into the URL itself
    assert "user123" not in req["url"]


def test_build_accounts_request_rejects_credless_url():
    with pytest.raises(sf.SimpleFINError):
        sf.build_accounts_request("https://bridge.example.com/simplefin")


def test_to_import_rows_shapes_and_skips_pending():
    rows, errors = sf.to_import_rows(SF_DATA["accounts"][0])
    assert errors == []
    assert [r["fitid"] for r in rows] == ["TXN-1", "TXN-2"]
    assert rows[0]["amount"] == Decimal("-55.50")
    assert rows[0]["date"] == date(2026, 8, 10)
    assert rows[0]["payee"] == "Johns Fishin Shack"


def test_to_import_rows_flags_malformed():
    rows, errors = sf.to_import_rows(
        {"name": "X", "transactions": [{"id": "T", "posted": "nope", "amount": "1"}]}
    )
    assert rows == []
    assert len(errors) == 1
    assert "nope" not in errors[0]  # message stays generic, no raw data echo


def test_parse_account_map_tolerates_junk():
    assert sf.parse_account_map('{"A": 3, "B": "4", "C": "x"}') == {"A": 3, "B": 4}
    assert sf.parse_account_map("not json") == {}
    assert sf.parse_account_map("[1,2]") == {}


# ---------------------------------------------------------------------------
# Claim + fetch against a mocked transport
# ---------------------------------------------------------------------------


def test_claim_access_url(monkeypatch):
    monkeypatch.setattr(sf, "send", lambda req, **kw: _resp(text=ACCESS_URL + "\n"))
    assert sf.claim_access_url(_token()) == ACCESS_URL


def test_a_token_from_another_simplefin_provider_is_claimed_at_its_own_host(
    monkeypatch,
):
    # #181: tokens are not only issued by bridge.simplefin.org. The claim URL
    # inside the token decides the host; the path shape is the provider's.
    other_claim = "https://sync.provider.example/api/sfin/claim/7f3a"
    other_access = "https://u:p@sync.provider.example/api/sfin"
    seen = {}

    def fake_send(req, **kw):
        seen.update(req)
        return _resp(text=other_access)

    monkeypatch.setattr(sf, "send", fake_send)
    assert sf.claim_access_url(_token(other_claim)) == other_access
    assert seen["url"] == other_claim
    req = sf.build_accounts_request(other_access)
    assert req["url"] == "https://sync.provider.example/api/sfin/accounts"


def test_claim_access_url_rejected_token(monkeypatch):
    monkeypatch.setattr(sf, "send", lambda req, **kw: _resp(status=403))
    with pytest.raises(sf.SimpleFINError):
        sf.claim_access_url(_token())


def test_fetch_accounts_error_paths(monkeypatch):
    monkeypatch.setattr(sf, "send", lambda req, **kw: _resp(status=500))
    with pytest.raises(sf.SimpleFINError):
        sf.fetch_accounts(ACCESS_URL)
    monkeypatch.setattr(sf, "send", lambda req, **kw: _resp(text="<html>"))
    with pytest.raises(sf.SimpleFINError):
        sf.fetch_accounts(ACCESS_URL)


# ---------------------------------------------------------------------------
# Sync: dedup + bank rules through the shared OFX path
# ---------------------------------------------------------------------------


def test_sync_imports_dedups_and_applies_rules(db_session):
    ba = _mk_bank_account(db_session)
    expense = Account(name="Bait Expense", account_type=AccountType.EXPENSE)
    db_session.add(expense)
    db_session.commit()
    db_session.add(
        BankRule(
            name="bait",
            pattern="fishin",
            rule_type="contains",
            account_id=expense.id,
            priority=10,
            is_active=True,
        )
    )
    db_session.commit()

    result = sf.sync_accounts(db_session, SF_DATA, {"ACT-1": ba.id})
    assert result["imported"] == 2
    assert result["skipped"] == 0
    assert result["warnings"] == []

    txns = (
        db_session.query(BankTransaction)
        .filter(BankTransaction.bank_account_id == ba.id)
        .all()
    )
    assert {t.import_id for t in txns} == {"TXN-1", "TXN-2"}
    assert all(t.import_source == "simplefin" for t in txns)
    bait = next(t for t in txns if t.import_id == "TXN-1")
    assert bait.match_status == "unmatched"  # rules categorise; adding is a click
    assert bait.category_account_id == expense.id

    # Second sync of the same window: everything dedups
    again = sf.sync_accounts(db_session, SF_DATA, {"ACT-1": ba.id})
    assert again["imported"] == 0
    assert again["skipped"] == 2


def test_sync_warns_on_missing_mapped_account(db_session):
    ba = _mk_bank_account(db_session)
    result = sf.sync_accounts(db_session, SF_DATA, {"GONE": ba.id})
    assert result["imported"] == 0
    assert len(result["warnings"]) == 1


# ---------------------------------------------------------------------------
# Sign direction: a positive amount is a deposit, whatever the feed is named
# ---------------------------------------------------------------------------

# A checking account as one bank connection presents it: named like a
# liability, balance negative, every amount with the opposite sign.
REVERSED_CHECKING = {
    "id": "ACT-4723",
    "name": "Payable Business Checking (4723)",
    "org": {"name": "Demo Bank"},
    "currency": "USD",
    "balance": "-3915.66",
    "transactions": [
        {
            "id": "TXN-BILL",
            "posted": 1788868800,  # 2026-09-08 UTC
            "amount": "2689.93",  # a bill payment, reported as money in
            "description": "Bill payment",
            "payee": "City Water",
        },
        {
            "id": "TXN-WIRE",
            "posted": 1788955200,  # 2026-09-09 UTC
            "amount": "-20000.00",  # an incoming transfer, reported as money out
            "description": "Incoming transfer",
            "payee": "Transfer in",
        },
    ],
}


def test_to_import_rows_reverses_signs_when_asked():
    rows, errors = sf.to_import_rows(SF_DATA["accounts"][0], reverse_signs=True)
    assert errors == []
    assert [r["amount"] for r in rows] == [Decimal("55.50"), Decimal("-2500.00")]


def test_an_account_name_never_changes_the_sign():
    rows, _ = sf.to_import_rows(REVERSED_CHECKING)
    assert [r["amount"] for r in rows] == [Decimal("2689.93"), Decimal("-20000.00")]


def test_parse_reversed_tolerates_junk():
    assert sf.parse_reversed("") == []
    assert sf.parse_reversed("not json") == []
    assert sf.parse_reversed('{"ACT-1": true}') == []
    assert sf.parse_reversed('["ACT-1", 7, null]') == ["ACT-1", "7"]


def test_a_reversed_liability_named_feed_lands_right_on_an_asset_account(
    client, db_session, seed_accounts
):
    """Regression: a checking account the bank connection reports as
    "Payable ... (4723)" with a negative balance, mapped to 1000 Checking.
    Imported as-is, the bill payment came in as a deposit and nothing
    auto-matched; with Reverse signs it lands as a withdrawal and matches
    the expense already posted for it."""
    feed = client.post(
        "/api/banking/accounts",
        json={"name": "Checking 4723", "account_id": seed_accounts["1000"].id},
    ).json()
    vendor = client.post("/api/vendors", json={"name": "City Water"}).json()
    r = client.post(
        "/api/expenses",
        json={
            "date": "2026-09-08",
            "vendor_id": vendor["id"],
            "expense_account_id": seed_accounts["6000"].id,
            "paid_from_account_id": seed_accounts["1000"].id,
            "amount": "2689.93",
        },
    )
    assert r.status_code == 201, r.text
    expense = r.json()
    data = {"errors": [], "accounts": [REVERSED_CHECKING]}

    result = sf.sync_accounts(
        db_session, data, {"ACT-4723": feed["id"]}, reversed_ids={"ACT-4723"}
    )
    assert result["imported"] == 2

    lines = {
        t.import_id: t
        for t in db_session.query(BankTransaction)
        .filter(BankTransaction.bank_account_id == feed["id"])
        .all()
    }
    assert lines["TXN-BILL"].amount == Decimal("-2689.93")
    assert lines["TXN-WIRE"].amount == Decimal("20000.00")
    assert lines["TXN-BILL"].match_status == "auto"
    assert lines["TXN-BILL"].transaction_id == expense["id"]


def test_without_reversal_the_same_feed_does_not_match(
    client, db_session, seed_accounts
):
    feed = client.post(
        "/api/banking/accounts",
        json={"name": "Checking 4723", "account_id": seed_accounts["1000"].id},
    ).json()
    vendor = client.post("/api/vendors", json={"name": "City Water"}).json()
    client.post(
        "/api/expenses",
        json={
            "date": "2026-09-08",
            "vendor_id": vendor["id"],
            "expense_account_id": seed_accounts["6000"].id,
            "paid_from_account_id": seed_accounts["1000"].id,
            "amount": "2689.93",
        },
    )
    data = {"errors": [], "accounts": [REVERSED_CHECKING]}
    sf.sync_accounts(db_session, data, {"ACT-4723": feed["id"]})
    bill = (
        db_session.query(BankTransaction)
        .filter(BankTransaction.import_id == "TXN-BILL")
        .one()
    )
    assert bill.amount == Decimal("2689.93")
    assert bill.match_status == "unmatched"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_claim_route_stores_secret_and_returns_accounts(
    authed_client, db_session, monkeypatch
):
    responses = [_resp(text=ACCESS_URL), _resp(json_body=SF_DATA)]
    monkeypatch.setattr(sf, "send", lambda req, **kw: responses.pop(0))
    r = authed_client.post("/api/simplefin/claim", json={"setup_token": _token()})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["accounts"][0]["id"] == "ACT-1"
    assert get_setting_raw(db_session, "simplefin_access_url") == ACCESS_URL

    # The access URL is a credential: GET /api/settings must redact it
    settings = authed_client.get("/api/settings").json()
    assert ACCESS_URL not in json.dumps(settings)
    assert settings["simplefin_access_url"] == "********"


def test_claim_route_bad_token_is_400(authed_client):
    r = authed_client.post(
        "/api/simplefin/claim", json={"setup_token": "!!definitely not base64!!"}
    )
    assert r.status_code == 400


def test_map_route_validates_bank_account(authed_client, db_session):
    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    db_session.commit()
    r = authed_client.post("/api/simplefin/map", json={"mapping": {"ACT-1": 99999}})
    assert r.status_code == 404

    ba = _mk_bank_account(db_session, name="Mapped Checking")
    r = authed_client.post(
        "/api/simplefin/map", json={"mapping": {"ACT-1": ba.id, "ACT-2": 0}}
    )
    assert r.status_code == 200
    assert r.json()["account_map"] == {"ACT-1": ba.id}


def test_sync_route_requires_connection_and_mapping(authed_client, db_session):
    r = authed_client.post("/api/simplefin/sync")
    assert r.status_code == 400  # not connected

    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    db_session.commit()
    r = authed_client.post("/api/simplefin/sync")
    assert r.status_code == 400  # connected but nothing mapped


def test_sync_route_end_to_end(authed_client, db_session, monkeypatch):
    ba = _mk_bank_account(db_session, name="Synced Checking")
    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    set_setting(db_session, "simplefin_account_map", json.dumps({"ACT-1": ba.id}))
    db_session.commit()
    monkeypatch.setattr(sf, "send", lambda req, **kw: _resp(json_body=SF_DATA))

    r = authed_client.post("/api/simplefin/sync")
    assert r.status_code == 200
    assert r.json()["imported"] == 2
    assert get_setting_raw(db_session, "simplefin_last_sync")

    status = authed_client.get("/api/simplefin/status").json()
    assert status["connected"] is True
    assert status["accounts"][0]["name"] == "Demo Checking"
    assert status["account_map"] == {"ACT-1": ba.id}


def test_map_route_stores_reversed_feeds_and_sync_applies_them(
    authed_client, db_session, monkeypatch
):
    ba = _mk_bank_account(db_session, name="Reversed Checking")
    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    db_session.commit()
    r = authed_client.post(
        "/api/simplefin/map",
        json={"mapping": {"ACT-1": ba.id}, "reversed": ["ACT-1"]},
    )
    assert r.status_code == 200
    assert r.json()["reversed"] == ["ACT-1"]

    # A mapping saved without the field leaves the choice alone
    r = authed_client.post("/api/simplefin/map", json={"mapping": {"ACT-1": ba.id}})
    assert r.json()["reversed"] == ["ACT-1"]
    assert authed_client.get("/api/simplefin/status").json()["reversed"] == ["ACT-1"]

    monkeypatch.setattr(sf, "send", lambda req, **kw: _resp(json_body=SF_DATA))
    assert authed_client.post("/api/simplefin/sync").status_code == 200
    amounts = {
        t.import_id: t.amount
        for t in db_session.query(BankTransaction)
        .filter(BankTransaction.bank_account_id == ba.id)
        .all()
    }
    assert amounts == {"TXN-1": Decimal("55.50"), "TXN-2": Decimal("-2500.00")}

    authed_client.post("/api/simplefin/disconnect")
    assert authed_client.get("/api/simplefin/status").json()["reversed"] == []


def test_sync_route_bridge_down_is_502(authed_client, db_session, monkeypatch):
    ba = _mk_bank_account(db_session, name="Down Checking")
    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    set_setting(db_session, "simplefin_account_map", json.dumps({"ACT-1": ba.id}))
    db_session.commit()
    monkeypatch.setattr(sf, "send", lambda req, **kw: _resp(status=500))
    r = authed_client.post("/api/simplefin/sync")
    assert r.status_code == 502


def _bearer(token):
    """Fresh cookie-less client so the Bearer (token) path authenticates,
    not the admin session — mirrors tests/test_api_tokens.py."""
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def test_connection_management_is_admin_only(authed_client, db_session, monkeypatch):
    """Claiming, remapping and disconnecting the bank feed change stored
    credentials and the books' wiring — an administrator's, like other
    connection settings (GET /api/settings redacts the access URL and a
    non-admin is refused writes there). A bookkeeper may sync an
    already-connected feed, which is daily books work, but must not connect,
    remap or disconnect it; a read-only token is refused every write."""
    keeper_token = authed_client.post(
        "/api/tokens", json={"label": "keeper", "role": "bookkeeper"}
    ).json()["token"]
    reader_token = authed_client.post(
        "/api/tokens", json={"label": "reader", "role": "readonly"}
    ).json()["token"]
    keeper = _bearer(keeper_token)
    reader = _bearer(reader_token)

    # An admin has already connected and mapped a feed.
    ba = _mk_bank_account(db_session, name="Shared Checking")
    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    set_setting(db_session, "simplefin_account_map", json.dumps({"ACT-1": ba.id}))
    db_session.commit()

    # Connection management is refused for both non-admin roles. The claim's
    # outbound POST must never fire, so no network mock is set: a 403 has to
    # come from the gate, before claim_access_url would reach send().
    for who in (keeper, reader):
        assert (
            who.post("/api/simplefin/claim", json={"setup_token": _token()}).status_code
            == 403
        )
        assert (
            who.post(
                "/api/simplefin/map", json={"mapping": {"ACT-1": ba.id}}
            ).status_code
            == 403
        )
        assert who.post("/api/simplefin/disconnect").status_code == 403

    # The refused calls changed nothing: the feed is still connected and mapped.
    assert get_setting_raw(db_session, "simplefin_access_url") == ACCESS_URL

    # A bookkeeper may sync the connected feed and read status; read-only may
    # read status but not sync (any write).
    monkeypatch.setattr(sf, "send", lambda req, **kw: _resp(json_body=SF_DATA))
    assert keeper.get("/api/simplefin/status").status_code == 200
    assert keeper.post("/api/simplefin/sync").status_code == 200
    assert reader.get("/api/simplefin/status").status_code == 200
    assert reader.post("/api/simplefin/sync").status_code == 403

    # Admin (the session) still connects, maps and disconnects.
    responses = [_resp(text=ACCESS_URL), _resp(json_body=SF_DATA)]
    monkeypatch.setattr(sf, "send", lambda req, **kw: responses.pop(0))
    assert (
        authed_client.post(
            "/api/simplefin/claim", json={"setup_token": _token()}
        ).status_code
        == 200
    )
    assert (
        authed_client.post(
            "/api/simplefin/map", json={"mapping": {"ACT-1": ba.id}}
        ).status_code
        == 200
    )
    assert authed_client.post("/api/simplefin/disconnect").status_code == 200


def test_disconnect_route_clears_settings(authed_client, db_session):
    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    set_setting(db_session, "simplefin_account_map", '{"ACT-1": 1}')
    db_session.commit()
    r = authed_client.post("/api/simplefin/disconnect")
    assert r.status_code == 200
    assert (get_setting_raw(db_session, "simplefin_access_url") or "") == ""
    status = authed_client.get("/api/simplefin/status").json()
    assert status["connected"] is False


# ---------------------------------------------------------------------------
# SSRF guard — user-supplied bridge URLs must never reach private space
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/simplefin/claim/X",
        "https://10.0.0.5/simplefin/claim/X",
        "https://192.168.68.1/simplefin/claim/X",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/simplefin/claim/X",
        "http://bridge.example.com/simplefin/claim/X",
    ],
)
def test_ssrf_guard_rejects_non_public(url):
    with pytest.raises(sf.SimpleFINError):
        sf._assert_public_https(url)


def test_ssrf_guard_allows_public_literal():
    # Literal public IP: getaddrinfo resolves numerically, no DNS involved
    sf._assert_public_https("https://1.1.1.1/simplefin")


# ---------------------------------------------------------------------------
# DNS rebinding: the request must go to the address the guard approved.
# ---------------------------------------------------------------------------


def _fake_getaddrinfo(ip):
    def gai(host, port, *a, **kw):
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    return gai


class _Stream:
    def __init__(self, peer):
        self.peer = peer

    def get_extra_info(self, name):
        return self.peer if name == "server_addr" else None


def test_send_pins_the_connection_to_the_approved_address(monkeypatch):
    seen = {}
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))

    def fake_request(self, method, url, **kw):
        seen.update(
            method=method,
            url=str(url),
            headers=dict(self.headers),
            ext=kw.get("extensions"),
        )
        return httpx.Response(
            200,
            text="ok",
            extensions={"network_stream": _Stream(("93.184.216.34", 443))},
        )

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    r = sf.send(
        {"method": "GET", "url": "https://bridge.example.com/simplefin/accounts"}
    )
    assert r.status_code == 200
    assert seen["url"] == "https://93.184.216.34/simplefin/accounts"
    assert seen["headers"]["host"] == "bridge.example.com"
    assert seen["ext"] == {"sni_hostname": "bridge.example.com"}


def test_send_pins_ipv6_with_brackets_and_keeps_the_port(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo("2606:2800:220:1:248:1893:25c8:1946")
    )

    def fake_request(self, method, url, **kw):
        seen["url"] = str(url)
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    sf.send(
        {
            "method": "GET",
            "url": "https://bridge.example.com:8443/simplefin/accounts?x=1",
        }
    )
    assert (
        seen["url"]
        == "https://[2606:2800:220:1:248:1893:25c8:1946]:8443/simplefin/accounts?x=1"
    )


def test_send_refuses_when_the_socket_landed_on_a_private_peer(monkeypatch):
    """Belt and braces after connect: if the peer is not the public address
    the guard approved, the response is discarded."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda self, m, u, **kw: httpx.Response(
            200,
            text="secret",
            extensions={"network_stream": _Stream(("10.0.0.5", 443))},
        ),
    )
    with pytest.raises(sf.SimpleFINError):
        sf.send(
            {"method": "GET", "url": "https://bridge.example.com/simplefin/accounts"}
        )


def test_guard_returns_the_address_it_checked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert sf._assert_public_https("https://bridge.example.com/x") == "93.184.216.34"


class _ClosedStream:
    """What the real stream looks like once the client has closed: the
    2.9.3 gate found send() calling getpeername() on a dead socket."""

    def get_extra_info(self, name):
        raise OSError(9, "Bad file descriptor")


def test_send_survives_a_stream_that_cannot_report_its_peer(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda self, m, u, **kw: httpx.Response(
            200, text="ok", extensions={"network_stream": _ClosedStream()}
        ),
    )
    assert (
        sf.send({"method": "GET", "url": "https://bridge.example.com/x"}).status_code
        == 200
    )


def test_peer_is_read_before_the_client_closes(monkeypatch):
    """The peer check must run inside the client's context: a stream that
    only answers while the client is open must be enough."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    state = {"open": False}

    class _LiveStream:
        def get_extra_info(self, name):
            if not state["open"]:
                raise OSError(9, "Bad file descriptor")
            return ("93.184.216.34", 443)

    real_enter, real_exit = httpx.Client.__enter__, httpx.Client.__exit__

    def enter(self):
        state["open"] = True
        return real_enter(self)

    def exit_(self, *a):
        state["open"] = False
        return real_exit(self, *a)

    monkeypatch.setattr(httpx.Client, "__enter__", enter)
    monkeypatch.setattr(httpx.Client, "__exit__", exit_)
    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda self, m, u, **kw: httpx.Response(
            200, text="ok", extensions={"network_stream": _LiveStream()}
        ),
    )
    assert (
        sf.send({"method": "GET", "url": "https://bridge.example.com/x"}).status_code
        == 200
    )

    # and a private peer seen while open is still refused
    class _PrivateStream:
        def get_extra_info(self, name):
            return ("10.0.0.5", 443) if state["open"] else None

    monkeypatch.setattr(
        httpx.Client,
        "request",
        lambda self, m, u, **kw: httpx.Response(
            200, text="secret", extensions={"network_stream": _PrivateStream()}
        ),
    )
    with pytest.raises(sf.SimpleFINError):
        sf.send({"method": "GET", "url": "https://bridge.example.com/x"})


# ---------------------------------------------------------------------------
# Older history (2.18.0, #181 follow-up): the first sync reaches back 85
# days, because the reference Bridge refuses a longer request; a provider
# such as BankSync keeps a year. Fetch older history asks for up to
# MAX_HISTORY_MONTHS in slices the Bridge accepts.
# ---------------------------------------------------------------------------


def test_months_before_lands_on_the_same_day_or_the_months_last():
    assert sf.months_before(date(2026, 9, 26), 12) == date(2025, 9, 26)
    assert sf.months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert sf.months_before(date(2024, 3, 31), 1) == date(2024, 2, 29)
    assert sf.months_before(date(2026, 1, 15), 3) == date(2025, 10, 15)


def test_history_windows_cover_the_range_in_bridge_sized_slices():
    since, until = date(2025, 9, 26), date(2026, 9, 27)
    windows = sf.history_windows(since, until)
    assert windows[0][0] == since and windows[-1][1] == until
    for (start, end), nxt in zip(windows, windows[1:] + [(until, None)]):
        assert 0 < (end - start).days <= sf.HISTORY_WINDOW_DAYS < 90
        assert end == nxt[0]  # no gap, no overlap
    assert len(windows) == 5
    assert sf.history_windows(since, since) == []


def test_build_accounts_request_carries_an_end_date():
    req = sf.build_accounts_request(
        ACCESS_URL, start_date=date(2026, 1, 1), end_date=date(2026, 3, 27)
    )
    assert req["params"] == {"start-date": 1767225600, "end-date": 1774569600}


def _slice_server(calls, per_slice):
    """A fake SimpleFIN server answering each request from per_slice, in
    order, and recording its params."""

    def fake_send(req, **kw):
        calls.append(dict(req["params"]))
        return _resp(json_body=per_slice[len(calls) - 1])

    return fake_send


def _acct(txns, balance="10.00"):
    return {
        "id": "ACT-1",
        "name": "Demo Checking",
        "org": {"name": "Demo Bank"},
        "currency": "USD",
        "balance": balance,
        "transactions": txns,
    }


def _txn(txn_id, posted, amount):
    return {"id": txn_id, "posted": posted, "amount": amount, "payee": txn_id}


def test_fetch_history_merges_the_slices(monkeypatch):
    calls = []
    per_slice = [
        {"errors": [], "accounts": [_acct([_txn("OLD", 1759000000, "-5.00")])]},
        {
            "errors": ["Demo Bank needs attention"],
            "accounts": [
                _acct(
                    [
                        _txn("OLD", 1759000000, "-5.00"),  # both slices return it
                        _txn("MID", 1768000000, "-7.00"),
                    ]
                )
            ],
        },
        {
            "errors": ["Demo Bank needs attention"],
            "accounts": [_acct([_txn("NEW", 1776000000, "20.00")], balance="99.00")],
        },
    ]
    monkeypatch.setattr(sf, "send", _slice_server(calls, per_slice))
    data = sf.fetch_history(ACCESS_URL, date(2025, 9, 1), date(2026, 5, 1))
    assert data["requests"] == 3 == len(calls)
    assert [a["id"] for a in data["accounts"]] == ["ACT-1"]
    assert [t["id"] for t in data["accounts"][0]["transactions"]] == [
        "OLD",
        "MID",
        "NEW",
    ]
    assert data["accounts"][0]["balance"] == "99.00"  # the latest slice's
    assert data["errors"] == ["Demo Bank needs attention"]


def test_sync_route_fetches_older_history_in_slices(
    authed_client, db_session, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    ba = _mk_bank_account(db_session, name="History Checking")
    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    set_setting(db_session, "simplefin_account_map", json.dumps({"ACT-1": ba.id}))
    db_session.commit()
    calls = []
    per_slice = [
        {"errors": [], "accounts": [_acct([_txn(f"T{i}", 1759000000 + i, "-1.00")])]}
        for i in range(10)
    ]
    monkeypatch.setattr(sf, "send", _slice_server(calls, per_slice))

    r = authed_client.post("/api/simplefin/sync", json={"history_months": 12})
    assert r.status_code == 200, r.text
    today = date.today()
    since = sf.months_before(today, 12)
    assert r.json()["since"] == since.isoformat()
    assert r.json()["imported"] == len(calls) == 5

    def day(epoch):
        return datetime.fromtimestamp(epoch, tz=timezone.utc).date()

    assert day(calls[0]["start-date"]) == since
    assert day(calls[-1]["end-date"]) == today + timedelta(days=1)
    for params in calls:
        assert (day(params["end-date"]) - day(params["start-date"])).days <= 85

    # an ordinary sync afterwards is one request from the last sync, as before
    calls.clear()
    per_slice[:] = [SF_DATA]
    assert authed_client.post("/api/simplefin/sync").status_code == 200
    assert len(calls) == 1 and "end-date" not in calls[0]


@pytest.mark.parametrize(
    "body", [{"history_months": 0}, {"history_months": 25}, {"months": 12}]
)
def test_sync_route_refuses_a_history_it_cannot_fetch(authed_client, db_session, body):
    set_setting(db_session, "simplefin_access_url", ACCESS_URL)
    db_session.commit()
    r = authed_client.post("/api/simplefin/sync", json=body)
    assert r.status_code == 422, r.text


def test_the_banking_page_offers_older_history():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "app/static/js/banking.js").read_text(
        encoding="utf-8"
    )
    assert "BankingPage.showSimpleFINHistory()" in js
    assert "API.post('/simplefin/sync', { history_months: months })" in js
    assert '<option value="12" selected>12 months</option>' in js


def test_the_banking_page_offers_reverse_signs_per_feed():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "app/static/js/banking.js").read_text(
        encoding="utf-8"
    )
    assert 'class="simplefin-reverse"' in js
    assert "API.post('/simplefin/map', { mapping, reversed })" in js
