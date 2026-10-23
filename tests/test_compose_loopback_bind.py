"""Compose published ports stay on the loopback interface by default.

docker-compose.yml used to publish Postgres as `${POSTGRES_PORT:-5433}:5432`
— a two-part mapping Docker binds to 0.0.0.0, i.e. every interface — while
POSTGRES_PASSWORD defaults to `bookkeeper`. On any host with a routable
address that put a known-credential database on the LAN. The app port had the
same problem. Both are now bound to 127.0.0.1 by default (overridable via
POSTGRES_BIND / APP_BIND). The prod file publishes neither service.
"""

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = {"127.0.0.1", "::1"}


def _expand(value: str) -> str:
    """Resolve `${VAR:-default}` to its default and `${VAR}` to empty, so we
    can read what `docker compose up` binds with no .env present."""
    value = re.sub(r"\$\{(\w+):-([^}]*)\}", r"\2", value)
    value = re.sub(r"\$\{(\w+)\}", "", value)
    return value


def _host_ip(port_entry) -> str | None:
    """The bind address of a compose `ports` entry, or None if it publishes
    on all interfaces (a two-part `host:container` mapping)."""
    parts = _expand(str(port_entry)).split(":")
    if len(parts) == 3:
        return parts[0]
    return None  # host:container — no explicit interface => 0.0.0.0


def test_dev_compose_binds_published_ports_to_loopback():
    doc = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    published = {
        name: svc["ports"] for name, svc in doc["services"].items() if svc.get("ports")
    }
    assert published, "expected the dev compose to publish ports"
    for name, ports in published.items():
        for entry in ports:
            ip = _host_ip(entry)
            assert ip in LOOPBACK, (
                f"{name} publishes {entry!r} on a non-loopback interface "
                f"(resolved host ip {ip!r})"
            )


def test_prod_compose_publishes_no_ports():
    doc = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    for name, svc in doc["services"].items():
        assert not svc.get(
            "ports"
        ), f"prod {name} must not publish ports: {svc.get('ports')}"
