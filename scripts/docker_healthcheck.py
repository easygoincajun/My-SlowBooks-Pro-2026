#!/usr/bin/env python3
"""Docker HEALTHCHECK probe for the app container.

Hits the unauthenticated /health endpoint over plain HTTP on the loopback
interface. Uses only the stdlib (urllib) so the image doesn't have to carry
curl just for this.

Crucially it does NOT follow redirects. With FORCE_HTTPS=true the app answers
http://127.0.0.1:3001/health with a 307 to https://, and chasing that to a
port that isn't speaking TLS would fail — so the container would flap
unhealthy while the worker is perfectly alive. A liveness probe only needs to
know the process is answering, so any 2xx or 3xx counts as healthy; a 4xx/5xx
or a connection error is unhealthy.
"""

import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("APP_PORT", "3001")
URL = f"http://127.0.0.1:{PORT}/health"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Returning None turns a redirect into an HTTPError we can read the status
    # off, instead of following it to an unreachable https port.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check(url: str = URL, timeout: float = 3.0) -> int:
    """Return 0 (healthy) when /health answers with 2xx or 3xx, else 1."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        status = opener.open(url, timeout=timeout).status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:
        return 1
    return 0 if 200 <= status < 400 else 1


if __name__ == "__main__":
    sys.exit(check())
