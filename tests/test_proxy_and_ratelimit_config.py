"""Two config gaps that silently weaken security behind a reverse proxy.

Both were live in docker-compose.prod.yml, which explicitly puts the app
behind a TLS-terminating proxy:

1. The reverse proxy's X-Forwarded-For was never trusted. uvicorn parses it
   but trusts it only from `forwarded_allow_ips` (default 127.0.0.1). A proxy
   in another container has a different address, so every request reported the
   proxy's IP — collapsing the login rate limiter (keyed on client IP) into one
   shared bucket and writing the proxy address into the login_attempts and
   portal_accesses audit tables.

2. Rate-limit counters lived in slowapi's default per-process MemoryStorage,
   so with APP_WORKERS=4 the configured limit was silently 4x looser.

Neither is fatal, so the app warns rather than refuses. These tests pin the
warnings, the storage wiring and its startup health check, that the Docker
entrypoint leaves the forwarded-trust decision to uvicorn (which reads
FORWARDED_ALLOW_IPS from the environment itself), that client_ip() reads only
what uvicorn decided, and the key func's view of the forwarded client IP
through a trusted proxy.
"""

import importlib
from pathlib import Path

import pytest
from slowapi.util import get_remote_address
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[1]


# --- Rate-limit storage wiring -------------------------------------------


def test_limiter_defaults_to_memory_storage(monkeypatch):
    """Unset URI keeps today's behaviour — single-container dev / desktop."""
    monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
    import app.services.rate_limit as rl

    importlib.reload(rl)
    try:
        assert rl.RATE_LIMIT_STORAGE_URI == ""
        assert type(rl.limiter._storage).__name__ == "MemoryStorage"
    finally:
        importlib.reload(rl)


def test_limiter_honours_storage_uri(monkeypatch):
    """A configured URI is passed through to slowapi, not ignored."""
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "memory://custom")
    import app.services.rate_limit as rl

    importlib.reload(rl)
    try:
        assert rl.RATE_LIMIT_STORAGE_URI == "memory://custom"
        assert rl.limiter._storage_uri == "memory://custom"
    finally:
        monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
        importlib.reload(rl)


# --- Startup health check of configured storage ---------------------------


def test_storage_check_passes_for_reachable_storage(monkeypatch):
    import app.main as main
    import app.services.rate_limit as rl

    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "memory://custom")
    importlib.reload(rl)
    try:
        main._check_rate_limit_storage()  # no raise
    finally:
        monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
        importlib.reload(rl)


def test_storage_check_skipped_when_unset(monkeypatch):
    """Memory storage is never checked — nothing to reach."""
    import app.main as main
    import app.services.rate_limit as rl

    monkeypatch.setattr(rl, "RATE_LIMIT_STORAGE_URI", "")
    monkeypatch.setattr(
        rl.limiter._storage, "check", lambda: (_ for _ in ()).throw(AssertionError)
    )
    main._check_rate_limit_storage()


@pytest.mark.parametrize("failure", ["false", "raise"])
def test_storage_check_refuses_unreachable_storage(monkeypatch, failure):
    """A dead Redis must stop startup, not 500 every login later. The error
    names the variable and where it points, never the credentials."""
    import app.main as main
    import app.services.rate_limit as rl

    uri = "redis://user:s3cret@redis.internal:6379/0"
    monkeypatch.setattr(rl, "RATE_LIMIT_STORAGE_URI", uri)
    if failure == "false":
        monkeypatch.setattr(rl.limiter._storage, "check", lambda: False)
    else:

        def boom():
            raise ConnectionError("refused")

        monkeypatch.setattr(rl.limiter._storage, "check", boom)

    with pytest.raises(RuntimeError) as excinfo:
        main._check_rate_limit_storage()

    msg = str(excinfo.value)
    assert "RATE_LIMIT_STORAGE_URI" in msg
    assert "redis://redis.internal:6379" in msg
    assert "s3cret" not in msg and "user:" not in msg


# --- Entrypoint leaves forwarded trust to uvicorn -------------------------


def test_entrypoint_does_not_override_forwarded_trust():
    """uvicorn reads FORWARDED_ALLOW_IPS from the environment itself and
    --proxy-headers is on by default, so the entrypoint passes neither flag:
    a flag here would silently override the operator's setting."""
    text = (ROOT / "docker-entrypoint.sh").read_text()
    # Comments may name the flags to explain their absence; commands may not.
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert "--forwarded-allow-ips" not in code
    assert "--proxy-headers" not in code
    assert "uvicorn_proxy_args" not in text


# --- client_ip reads only what uvicorn decided ----------------------------
#
# The audit tables (login_attempts, portal_accesses) use client_ip(). It must
# not parse X-Forwarded-For itself: uvicorn already rewrote request.client when
# the peer is trusted, and on a directly reachable deploy the header is forged.


def _client_ip_app():
    from app.services.request_utils import client_ip

    return Starlette(routes=[Route("/", lambda r: PlainTextResponse(client_ip(r)))])


def test_client_ip_ignores_forwarded_header_and_uses_request_client():
    client = TestClient(_client_ip_app())
    resp = client.get("/", headers={"x-forwarded-for": "203.0.113.9"})
    assert resp.text == "testclient"


def test_client_ip_truncates_and_tolerates_no_client():
    from types import SimpleNamespace

    from app.services.request_utils import client_ip

    long_host = "a" * 60
    assert client_ip(SimpleNamespace(client=SimpleNamespace(host=long_host))) == (
        "a" * 45
    )
    assert client_ip(SimpleNamespace(client=None)) == ""


# --- Key func sees the forwarded client IP through a trusted proxy --------
#
# The rate limiter keys on get_remote_address, which reads request.client.host.
# uvicorn's ProxyHeadersMiddleware rewrites request.client from X-Forwarded-For
# only when the socket peer is trusted — this is the exact mechanism the
# entrypoint's --proxy-headers / --forwarded-allow-ips flags configure.


def _remote_addr_app(trusted_hosts):
    inner = Starlette(
        routes=[Route("/", lambda r: PlainTextResponse(get_remote_address(r)))]
    )
    return ProxyHeadersMiddleware(inner, trusted_hosts=trusted_hosts)


def test_key_func_sees_client_ip_from_trusted_proxy():
    # TestClient's socket peer is "testclient"; treat it as the trusted proxy.
    client = TestClient(_remote_addr_app(["testclient"]))
    resp = client.get("/", headers={"x-forwarded-for": "203.0.113.9"})
    assert resp.text == "203.0.113.9"


def test_key_func_ignores_forwarded_ip_from_untrusted_peer():
    # Peer not in the trust list -> the header is spoofable, so it is ignored
    # and the socket peer is used. This is why an empty trust list behind a
    # proxy keys every request to the proxy itself.
    client = TestClient(_remote_addr_app(["127.0.0.1"]))
    resp = client.get("/", headers={"x-forwarded-for": "203.0.113.9"})
    assert resp.text == "testclient"


# --- Compose file requirements -------------------------------------------


def test_prod_compose_passes_forwarded_allow_ips_without_requiring_it():
    """Prod compose hands FORWARDED_ALLOW_IPS through but does not refuse to
    start without it: an upgrade must not stop a running deployment. A blank
    value gets the startup warning instead (tested below)."""
    text = (ROOT / "docker-compose.prod.yml").read_text()
    assert 'FORWARDED_ALLOW_IPS: "${FORWARDED_ALLOW_IPS:-}"' in text
    assert "${FORWARDED_ALLOW_IPS:?" not in text


def test_prod_compose_exposes_rate_limit_storage():
    text = (ROOT / "docker-compose.prod.yml").read_text()
    assert "RATE_LIMIT_STORAGE_URI" in text


# --- Startup warnings -----------------------------------------------------


def test_warns_when_proxy_trust_unset(monkeypatch, caplog):
    import app.main as main

    monkeypatch.setattr(main, "FORCE_HTTPS", True)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)

    with caplog.at_level("WARNING", logger="uvicorn.error"):
        main._warn_on_proxy_misconfiguration()

    assert any("FORWARDED_ALLOW_IPS" in r.message for r in caplog.records)


def test_no_proxy_warning_when_trust_configured(monkeypatch, caplog):
    import app.main as main

    monkeypatch.setattr(main, "FORCE_HTTPS", True)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.18.0.0/16")

    with caplog.at_level("WARNING", logger="uvicorn.error"):
        main._warn_on_proxy_misconfiguration()

    assert not any("FORWARDED_ALLOW_IPS" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "workers,storage,expected",
    [
        ("1", "", False),  # single worker — memory storage is correct
        ("4", "redis://r:6379/0", False),  # shared storage — fine at any count
        ("4", "", True),  # the broken combination
    ],
)
def test_rate_limit_warning_matrix(monkeypatch, caplog, workers, storage, expected):
    import app.main as main
    import app.services.rate_limit as rl

    monkeypatch.setattr(main, "FORCE_HTTPS", False)  # isolate the storage warning
    monkeypatch.setenv("APP_WORKERS", workers)
    monkeypatch.setattr(rl, "RATE_LIMIT_STORAGE_URI", storage)
    monkeypatch.setattr(rl, "_enabled", True)

    with caplog.at_level("WARNING", logger="uvicorn.error"):
        main._warn_on_proxy_misconfiguration()

    warned = any("RATE_LIMIT_STORAGE_URI" in r.message for r in caplog.records)
    assert warned is expected


def test_no_storage_warning_when_rate_limiting_disabled(monkeypatch, caplog):
    """Disabled limiter (the test default) means per-worker counters don't
    matter, so don't nag about storage."""
    import app.main as main
    import app.services.rate_limit as rl

    monkeypatch.setattr(main, "FORCE_HTTPS", False)
    monkeypatch.setenv("APP_WORKERS", "4")
    monkeypatch.setattr(rl, "RATE_LIMIT_STORAGE_URI", "")
    monkeypatch.setattr(rl, "_enabled", False)

    with caplog.at_level("WARNING", logger="uvicorn.error"):
        main._warn_on_proxy_misconfiguration()

    assert not any("RATE_LIMIT_STORAGE_URI" in r.message for r in caplog.records)
