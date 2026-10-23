"""`.dockerignore` keeps runtime secrets and data out of the build context,
without excluding anything the image needs to run.

`Dockerfile` does `COPY . .`, so every path the build context carries lands
in a shared image. A developer's auto-generated keys (.slowbooks-master.key,
app/services/crypto.py:61; .slowbooks-session.key, app/services/auth.py:95),
SQLite company files, uploads, and backups must never ride along. At the same
time the things the container genuinely needs at runtime — alembic migrations,
app/static assets, index.html, requirements.txt, the entrypoint — must still
copy. This test pins both directions so a future `.dockerignore` edit can't
silently leak a secret or break the image.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _compile(pattern: str) -> re.Pattern:
    """Translate one Docker ignore pattern to a regex.

    Docker (moby/patternmatcher) uses filepath.Match semantics: `*` matches a
    run of non-separator chars, `**` crosses separators, `?` is one non-sep
    char. A trailing slash is a directory pattern that also matches everything
    beneath it — so we anchor with an optional `(/.*)?` tail.
    """
    pattern = pattern.rstrip("/")
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile(r"^" + "".join(out) + r"(/.*)?$")


def _rules() -> list[tuple[bool, re.Pattern]]:
    rules = []
    for raw in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        rules.append((negate, _compile(line[1:] if negate else line)))
    return rules


def _ignored(path: str) -> bool:
    """Last matching rule wins (Docker semantics); `!` re-includes."""
    result = False
    for negate, rx in _rules():
        if rx.match(path):
            result = not negate
    return result


# Secrets and runtime data that must NEVER enter the image.
EXCLUDED = [
    ".slowbooks-master.key",
    ".slowbooks-session.key",
    ".env",
    "bookkeeper.db",
    "app/services/companies/acme.db",
    "acme.db-wal",
    "acme.db-shm",
    "acme.db-journal",
    "data.sqlite3",
    "app/static/uploads",
    "app/static/uploads/logo.png",
    "backups/bookkeeper-2026-05-23.sql.gz",
]

# Things the running container needs — these must still copy in.
REQUIRED = [
    "alembic.ini",
    "migrations/env.py",
    "migrations/versions/0001_initial.py",
    "app/main.py",
    "app/static/css/app.css",
    "app/static/js/app.js",
    "app/static/favicon.ico",
    "index.html",
    "requirements.txt",
    "docker-entrypoint.sh",
    "scripts/seed_database.py",
    "scripts/docker_healthcheck.py",
]


def test_every_secret_and_data_path_is_excluded():
    missed = [p for p in EXCLUDED if not _ignored(p)]
    assert not missed, f".dockerignore does not cover: {missed}"


def test_nothing_the_runtime_needs_is_excluded():
    dropped = [p for p in REQUIRED if _ignored(p)]
    assert not dropped, f".dockerignore excludes runtime-required paths: {dropped}"
