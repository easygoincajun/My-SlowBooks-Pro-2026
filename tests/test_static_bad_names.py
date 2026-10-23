"""/static answers 404, not 500, for a name the filesystem can't hold
(2.26.0 gate, W-31: on Windows ':' or one of < > ? * " | raised an OSError
Starlette doesn't catch)."""

import pytest
from starlette.testclient import TestClient

from app import main


@pytest.mark.parametrize(
    "err", [OSError(22, "Invalid argument"), ValueError("embedded null byte")]
)
def test_a_name_the_filesystem_refuses_is_a_404(monkeypatch, err):
    from fastapi.staticfiles import StaticFiles

    def refuse(self, path):
        raise err

    monkeypatch.setattr(StaticFiles, "lookup_path", refuse)
    with TestClient(main.app) as c:
        r = c.get("/static/css/a*b")
    assert r.status_code == 404
