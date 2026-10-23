"""The Docker HEALTHCHECK probe treats the app as alive on 2xx AND on the
FORCE_HTTPS redirect.

The probe hits http://127.0.0.1:3001/health over plain HTTP. With
FORCE_HTTPS=true the app answers that with a 307 to https://; following it to
a non-TLS port would fail and the container would flap unhealthy even though
the worker is up. The probe must NOT follow the redirect — a 3xx still proves
liveness. A real failure (5xx, connection refused) must report unhealthy.
"""

import http.server
import threading

import pytest

from scripts.docker_healthcheck import check


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802 (stdlib naming)
        self.send_response(self.status)
        if self.status in (301, 302, 307, 308):
            self.send_header("Location", "https://127.0.0.1:3001/health")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):  # silence the test server
        pass


def _serve(status: int):
    handler = type("H", (_Handler,), {"status": status})
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/health"


@pytest.mark.parametrize("status", [200, 307, 308])
def test_healthy_on_2xx_and_redirect(status):
    srv, url = _serve(status)
    try:
        assert check(url, timeout=3) == 0
    finally:
        srv.shutdown()


@pytest.mark.parametrize("status", [500, 503])
def test_unhealthy_on_server_error(status):
    srv, url = _serve(status)
    try:
        assert check(url, timeout=3) == 1
    finally:
        srv.shutdown()


def test_unhealthy_when_nothing_listening():
    # Port 1 is never a live app; connection refused => unhealthy, not a crash.
    assert check("http://127.0.0.1:1/health", timeout=1) == 1
