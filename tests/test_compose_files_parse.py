"""Every compose file in the repo parses as YAML (#307, @bracycpa: Compose
2.40 refused docker-compose.prod.yml with "mapping values are not allowed
in this context" because two required-secret lines carried a ": " inside
an unquoted scalar). The values are quoted now; this keeps them so."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted(ROOT.glob("docker-compose*.yml")) + sorted(
    ROOT.glob("docker-compose*.yaml")
)


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_the_compose_file_parses(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and "services" in doc, path.name


def test_the_required_secrets_still_refuse_to_start_empty():
    doc = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    env = next(
        s
        for s in doc["services"].values()
        if "PAYROLL_ENCRYPTION_SECRET" in (s.get("environment") or {})
    )["environment"]
    for key in ("PAYROLL_ENCRYPTION_SECRET", "SESSION_SECRET_KEY"):
        value = env[key]
        assert value.startswith("${" + key + ":?") and "openssl" in value, (key, value)
        assert ": " not in value.strip("${}"), (
            key,
            value,
        )  # the character Compose tripped on
