# ============================================================================
# Company file encryption — the plumbing (issue #290).
#
# A desktop company is one SQLite file. Encrypted, it is a SQLCipher file:
# every page is encrypted under the driver, so nothing readable touches the
# disk, WAL and random access keep working, and a backup is a copy of
# ciphertext. SQLAlchemy takes the sqlcipher3 module in place of sqlite3,
# and the key goes onto every connection first, ahead of the PRAGMAs
# app/database.py already sets.
#
# This module is the one place that knows how a key is found, how it is
# put on a connection, how an encrypted file is told from a plain one, and
# how a file is converted either way. Nothing here decides policy: who may
# turn encryption on is the route's business (app/routes/company_file.py),
# and where a desktop key is kept between launches is the next release's
# (per-administrator wrapping and a recovery key, #290).
#
# Key sources, in order:
#   1. SLOWBOOKS_COMPANY_FILE_KEY — the raw 32-byte key as 64 hex characters.
#      The launcher resolves the key before it touches the file and hands it
#      to the server child and to the in-process migration this way.
#   2. COMPANY_FILE_ENCRYPTION_SECRET — an operator-set secret, the way
#      PAYROLL_ENCRYPTION_SECRET is set for Server Edition. The key is
#      derived from it the way app/services/encryption.py derives the
#      payroll key (PBKDF2-HMAC-SHA256, 480,000 rounds), under this file's
#      own salt, so the two secrets never share a key.
#   3. The key this process was given at runtime (set_runtime_key): the
#      desktop's own, derived from a recovery key and wrapped for each
#      administrator's password in a keyring beside the file
#      (app/services/company_keyring.py). The launcher unlocks it at the
#      picker and hands it on as (1); the switch that turns encryption on
#      in a running process keeps it here.
#
# The key reaches SQLCipher as a raw key (x'…'), which skips SQLCipher's own
# 256,000-round KDF on every connection: the derivation above is paid once,
# before the file is opened, not on every checkout from the pool.
#
# A plain company file stays plain. The engine applies the key only when
# the file on disk is encrypted, so an operator who sets the secret ahead
# of turning encryption on still opens the books, and the suite's in-memory
# database never sees a key at all.
# ============================================================================

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional, Union

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

SECRET_ENV = "COMPANY_FILE_ENCRYPTION_SECRET"
KEY_ENV = "SLOWBOOKS_COMPANY_FILE_KEY"

# Its own salt: the payroll secret derives under b"slowbooks-payroll-v1" and
# the settings key under HKDF with b"slowbooks-settings-key-v1". The same
# secret in COMPANY_FILE_ENCRYPTION_SECRET and PAYROLL_ENCRYPTION_SECRET
# would still give two different keys.
_SALT = b"slowbooks-company-file-v1"
_ROUNDS = 480_000
KEY_BYTES = 32

# A plain SQLite file begins with this; a SQLCipher file begins with its
# random salt, which is never these bytes.
SQLITE_HEADER = b"SQLite format 3\x00"

# One sentence each. These reach the person: a fatal startup line, the
# launcher's error box, or an HTTP 409. Never a traceback, never SQLCipher's
# own stderr lines (silenced on every connection; see _apply_key).
NO_KEY_MESSAGE = (
    "This company file is encrypted and no key was provided: set "
    "COMPANY_FILE_ENCRYPTION_SECRET to the secret it was encrypted with, "
    "then start SlowBooks Pro again."
)
WRONG_KEY_MESSAGE = (
    "This company file is encrypted and the key provided does not open it."
)
UNAVAILABLE_MESSAGE = (
    "This company file is encrypted and this build cannot open encrypted "
    "files: the sqlcipher3 module is missing."
)
POSTGRESQL_MESSAGE = (
    "This company runs on PostgreSQL, and encrypting it at rest is the "
    "database host's job: PostgreSQL's own disk encryption, or the volume "
    "it sits on."
)


class CompanyFileLocked(Exception):
    """An encrypted company file could not be opened: no key, the wrong key,
    or no SQLCipher in this build. ``str(exc)`` is the one sentence to show."""


class CompanyFileBusy(CompanyFileLocked):
    """The rewritten file could not take the company file's place: another
    program holds the file open. ``str(exc)`` is the sentence to show."""


BUSY_MESSAGE = (
    "The company file could not be replaced: another program has it open (a "
    "backup, sync or antivirus tool, or another SlowBooks Pro). Close it and "
    "try again; the books are as they were."
)


KeyLike = Union[bytes, Callable[[], Optional[bytes]], None]


# ---------------------------------------------------------------------------
# The module
# ---------------------------------------------------------------------------


def _sqlcipher():
    """The sqlcipher3 module, or None when this environment lacks it."""
    try:
        import sqlcipher3
    except ImportError:
        return None
    return sqlcipher3


def available() -> bool:
    return _sqlcipher() is not None


def cipher_version() -> str | None:
    """``PRAGMA cipher_version`` through the module as it is actually
    loaded — in the frozen bundle, the proof that PyInstaller carried it."""
    mod = _sqlcipher()
    if mod is None:
        return None
    conn = mod.connect(":memory:")
    try:
        row = conn.execute("PRAGMA cipher_version").fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


# Everything a raw connection can raise, from either module, plus our own.
def errors() -> tuple:
    mod = _sqlcipher()
    kinds: list = [sqlite3.Error, CompanyFileLocked]
    if mod is not None:
        kinds.append(mod.Error)
    return tuple(kinds)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def derive_key(secret: str) -> bytes:
    """The raw 32-byte key for an operator-set secret. Deterministic: the
    same secret opens the same file on every start."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=_SALT, iterations=_ROUNDS
    )
    return kdf.derive(secret.encode("utf-8"))


def _secret() -> str:
    """COMPANY_FILE_ENCRYPTION_SECRET as the app sees it. app.config loads
    the .env the app reads (the desktop's lives in the data dir, named by
    SLOWBOOKS_ENV_FILE) into the environment on import, so the launcher
    sees a secret kept there whichever module imported first."""
    try:
        import app.config  # noqa: F401 — for load_dotenv's side effect
    except Exception:
        pass
    return (os.environ.get(SECRET_ENV) or "").strip()


def _env_key() -> bytes | None:
    raw = (os.environ.get(KEY_ENV) or "").strip()
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        logger.error("%s is not hexadecimal; ignoring it", KEY_ENV)
        return None
    if len(key) != KEY_BYTES:
        logger.error("%s is not %d bytes; ignoring it", KEY_ENV, KEY_BYTES)
        return None
    return key


# The key this process was handed at runtime (#290): the desktop's, from
# the keyring, kept for the process and never written anywhere.
_runtime_key: bytes | None = None
# Bumped whenever the runtime key changes (a new recovery key re-keys the
# file under another key): an engine that cached the key it resolved looks
# again, rather than keying its next connection with the old one.
_key_generation = 0


def set_runtime_key(key: bytes | None) -> None:
    global _runtime_key, _key_generation
    if key is not None and len(key) != KEY_BYTES:
        raise ValueError(f"a company file key is {KEY_BYTES} bytes")
    _runtime_key = key
    _key_generation += 1


def _has_keyring() -> bool:
    try:
        from app.services import company_keyring

        return company_keyring.exists(active_path())
    except Exception:
        return False


def key_source() -> str | None:
    """Where the key comes from, as the person set it up: "secret"
    (COMPANY_FILE_ENCRYPTION_SECRET, wherever SlowBooks Pro starts),
    "keyring" (the desktop's own: a recovery key, wrapped for each
    administrator's sign-in beside the file), "launcher" (a raw key the
    launcher handed this process from somewhere else), or None. The
    launcher derives its key from the secret and hands it over, so with
    the secret set the answer is the secret, not the launcher (2.23.2
    gate, W-2 / NEW-108); with a keyring beside the file, the keyring."""
    if _secret():
        return "secret"
    if _has_keyring():
        return "keyring"
    if serving_another_company():
        return "launcher" if _env_key() is not None else None
    if _env_key() is not None or _runtime_key is not None:
        return "launcher"
    return None


def install_key() -> bytes | None:
    """The key every company on this install shares, when there is one: the
    one COMPANY_FILE_ENCRYPTION_SECRET derives, or the launcher's copy of it
    in the environment. A Server Edition server opens the other companies
    it serves with this one only (#358): the runtime key is the started
    company's own."""
    key = _env_key()
    if key is not None:
        return key
    secret = _secret()
    if secret:
        return _derived(secret)
    return None


_derived_keys: dict = {}


def _derived(secret: str) -> bytes:
    """derive_key, once per secret per process: it costs about half a
    second, and every other company's engine asks."""
    found = _derived_keys.get(secret)
    if found is None:
        found = _derived_keys[secret] = derive_key(secret)
    return found


def resolve_key() -> bytes | None:
    """The key in hand, or None. Derivation from the secret costs about half
    a second; callers that may not need it pass this function itself to
    ``create_sqlite_engine`` and it runs only for an encrypted file.

    A key set at runtime comes first: it is the newest knowledge. A new
    recovery key re-keys the file and sets it, while the key the launcher
    handed this process in its environment is the retired one; preferring
    the environment left every connection after a re-key on the old key
    until a restart (2.24.0 gate, NEW-157)."""
    if serving_another_company():
        return install_key()
    if _runtime_key is not None:
        return _runtime_key
    key = _env_key()
    if key is not None:
        return key
    secret = _secret()
    if secret:
        return derive_key(secret)
    return None


def _key_pragma(key: bytes) -> str:
    if len(key) != KEY_BYTES:
        raise ValueError(f"a company file key is {KEY_BYTES} bytes")
    return f"PRAGMA key = \"x'{key.hex()}'\""


def _resolve(key: KeyLike) -> bytes | None:
    return key() if callable(key) else key


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def sqlite_path(url: str) -> Path | None:
    """The file behind a ``sqlite:///`` URL; None for :memory: or another
    database."""
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///") :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def active_path() -> Path | None:
    """The file behind the company this request works in (the running app's
    DATABASE_URL, or another company a Server Edition session chose: #358),
    when it is one."""
    from app.config import DATABASE_URL
    from app.database import another_company_url

    return sqlite_path(another_company_url() or DATABASE_URL)


def serving_another_company() -> bool:
    """True while a request works in a company other than the one the
    server was started for (#358)."""
    from app.database import served_company, started_company_file

    chosen = served_company()
    return chosen is not None and chosen != started_company_file()


def is_encrypted_file(path: Path | str | None) -> bool:
    """True when the file exists, has content, and is not plain SQLite. A
    file that does not exist yet is not encrypted: it is whatever its first
    writer makes it."""
    if path is None:
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(len(SQLITE_HEADER))
    except OSError:
        return False
    if len(head) < len(SQLITE_HEADER):
        return False
    return not head.startswith(SQLITE_HEADER)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def _apply_key(dbapi_conn, key: bytes) -> None:
    """Key a fresh SQLCipher connection and prove the key opens the file.

    The key must be the first statement on the connection. SQLCipher writes
    its own lines to stderr when a key is wrong (``ERROR CORE
    sqlcipher_page_cipher: hmac check failed``); the log level is turned
    off first so the person sees one sentence, not the driver's diary."""
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA cipher_log_level = NONE")
        cur.execute(_key_pragma(key))
        try:
            cur.execute("SELECT count(*) FROM sqlite_master")
            cur.fetchone()
        except Exception as exc:  # the module's DatabaseError: not a database
            raise CompanyFileLocked(WRONG_KEY_MESSAGE) from exc
    finally:
        cur.close()


def sqlite_module():
    """The one SQLite library this process opens company files with: the
    sqlcipher3 module when it is installed (the engine's), else the
    standard sqlite3. Never both on one file (2.24.0 gate, NEW-156): the
    engine kept every plain company file open through sqlcipher3 while a
    backup, or the copy for an accountant, read it through the standard
    module. Two SQLite libraries in one process do not see each other's
    connections, so the second one, closing, took itself for the last
    connection, checkpointed the WAL and deleted it under the engine, and
    everything the engine wrote from then on went when the server exited
    (sqlite.org, "How To Corrupt", 2.2). Every release from 2.23.2 to
    2.23.5 had it on a plain file; an encrypted file was never affected,
    both of its connections being sqlcipher3's."""
    return _sqlcipher() or sqlite3


def connect_raw(path: Path, key: bytes | None = None, *, readonly: bool = False):
    """A DB-API connection to a company file, keyed when ``key`` is given.

    With a key, the connection is SQLCipher's and the file is read or
    written encrypted (a file that does not exist yet is created encrypted).
    Without one, the file is read or written plain, through the same
    library the engine uses (sqlite_module) — unless the file is encrypted,
    which raises CompanyFileLocked rather than open ciphertext as a
    database.

    ``readonly`` opens the file as it lies (``mode=ro&immutable=1``): no lock,
    no sidecar files, the way backup facts are read."""
    path = Path(path)
    if key is None and is_encrypted_file(path):
        raise CompanyFileLocked(NO_KEY_MESSAGE)
    if key is not None:
        mod = _sqlcipher()
        if mod is None:
            raise CompanyFileLocked(UNAVAILABLE_MESSAGE)
    else:
        mod = sqlite_module()
    if readonly:
        target = path.resolve().as_uri() + "?mode=ro&immutable=1"
        conn = mod.connect(target, uri=True)
    else:
        conn = mod.connect(str(path))
    if key is not None:
        try:
            _apply_key(conn, key)
        except Exception:
            conn.close()
            raise
    return conn


def create_sqlite_engine(url: str, key: KeyLike = None, **engine_kwargs):
    """A SQLAlchemy engine for a SQLite URL that opens an encrypted file.

    The engine uses sqlcipher3 in place of sqlite3 when it is installed
    (a plain file opens through it exactly as before). On every new
    connection, if the file on disk is encrypted, the key goes on first —
    before app/database.py's PRAGMAs, which register after this one. ``key``
    may be the bytes, or a callable that finds them, called once and only
    for an encrypted file; an encrypted file with no key raises
    CompanyFileLocked with the sentence to show."""
    from sqlalchemy import create_engine, event

    mod = _sqlcipher()
    if mod is not None:
        engine_kwargs.setdefault("module", mod)
    engine = create_engine(url, **engine_kwargs)
    path = sqlite_path(url)
    if path is None:
        return engine  # :memory: — nothing on disk to be encrypted

    resolved: dict = {}

    @event.listens_for(engine, "connect", insert=True)
    def _unlock(dbapi_conn, _record):
        if not is_encrypted_file(path):
            return
        if mod is None:
            raise CompanyFileLocked(UNAVAILABLE_MESSAGE)
        # looked up once it is found; a file that was plain when the
        # engine was made and is encrypted now (the switch, #290) resolves
        # on its first encrypted connection, and a key handed to the
        # process later is found then, not refused for good
        if resolved.get("key") is None or resolved.get("generation") != _key_generation:
            resolved["key"] = _resolve(key)
            resolved["generation"] = _key_generation
        if resolved["key"] is None:
            raise CompanyFileLocked(NO_KEY_MESSAGE)
        _apply_key(dbapi_conn, resolved["key"])

    return engine


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def convert(path: Path, key: bytes | None, *, encrypt: bool) -> None:
    """Rewrite a company file in place, encrypted (``encrypt=True``) or plain.

    SQLCipher's own export: the file is read through its codec and written
    page by page into a sibling file in the other state, which then
    replaces it. A write lock is held on the source for the duration, so a
    concurrent writer waits and then fails rather than lose its change
    silently. The caller disposes any engine pool on the file first; on
    Windows an open handle refuses the replace."""
    path = Path(path)
    source_encrypted = is_encrypted_file(path)
    if source_encrypted == encrypt:
        return
    if key is None:
        raise CompanyFileLocked(NO_KEY_MESSAGE)
    mod = _sqlcipher()
    if mod is None:
        raise CompanyFileLocked(UNAVAILABLE_MESSAGE)

    target = path.with_name(path.name + ".converting")
    for stale in (target, Path(str(target) + "-journal")):
        stale.unlink(missing_ok=True)

    # isolation_level=None: the module's own transaction management stays
    # out of the way; ATTACH is refused inside a transaction, so the order
    # below is attach, lock, export, commit, detach.
    conn = mod.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA cipher_log_level = NONE")
        if source_encrypted:
            conn.execute(_key_pragma(key))
            try:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except Exception as exc:
                raise CompanyFileLocked(WRONG_KEY_MESSAGE) from exc
        target_key = f"x'{key.hex()}'" if encrypt else ""
        conn.execute(
            "ATTACH DATABASE ? AS target KEY ?", (target.as_posix(), target_key)
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("SELECT sqlcipher_export('target')").fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("DETACH DATABASE target")
        # Fold the WAL into the file we are about to replace, so the
        # sidecars left beside it are empty and safe to remove.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except Exception:
        conn.close()
        target.unlink(missing_ok=True)
        raise
    conn.close()

    # On Windows a reader holding the file refuses the replace. Try for a
    # moment, then give the file up: a failed Turn off once left the whole
    # company as a plain .converting copy beside the encrypted file, readable
    # with no key (2.23.2 gate, W-1).
    last: Exception | None = None
    for attempt in range(6):
        try:
            os.replace(target, path)
            last = None
            break
        except OSError as exc:
            last = exc
            time.sleep(0.25 * (attempt + 1))
    if last is not None:
        target.unlink(missing_ok=True)
        logger.warning("company file %s could not be replaced: %s", path.name, last)
        raise CompanyFileBusy(BUSY_MESSAGE) from last
    for sidecar in ("-wal", "-shm"):
        Path(str(path) + sidecar).unlink(missing_ok=True)
    logger.info(
        "company file %s is now %s", path.name, "encrypted" if encrypt else "plain"
    )


def rekey(path: Path, old_key: bytes, new_key: bytes) -> None:
    """Re-encrypt a company file in place under another key (a new recovery
    key, #290): SQLCipher's own ``PRAGMA rekey`` rewrites every page. The
    caller disposes any engine pool on the file first, as for convert."""
    path = Path(path)
    if not is_encrypted_file(path):
        raise CompanyFileLocked(NO_KEY_MESSAGE)
    mod = _sqlcipher()
    if mod is None:
        raise CompanyFileLocked(UNAVAILABLE_MESSAGE)
    conn = mod.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA cipher_log_level = NONE")
        conn.execute(_key_pragma(old_key))
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception as exc:
            raise CompanyFileLocked(WRONG_KEY_MESSAGE) from exc
        conn.execute(f"PRAGMA rekey = \"x'{new_key.hex()}'\"")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        conn.close()
    logger.info("company file %s re-keyed", path.name)


def sweep_stale_conversions(path: Path | str | None) -> list[str]:
    """Remove what an interrupted rewrite could leave on disk: a
    ``.converting`` file beside the company file (a plain copy of
    encrypted books, or the other way round) and a ``.restoring`` staged
    copy in the backups folder. Returns the names removed. Run at every
    start, before anything opens the file."""
    removed: list[str] = []
    if path is not None:
        path = Path(path)
        for stale in (
            path.with_name(path.name + ".converting"),
            path.with_name(path.name + ".converting-journal"),
        ):
            if stale.exists():
                stale.unlink(missing_ok=True)
                removed.append(stale.name)
    try:
        from app.services import storage

        folder = storage.backups_root()
    except Exception:  # pragma: no cover - no backups root in this context
        folder = None
    if folder is not None and Path(folder).is_dir():
        for stale in Path(folder).glob("*.restoring"):
            stale.unlink(missing_ok=True)
            removed.append(stale.name)
    if removed:
        logger.warning(
            "removed what an interrupted rewrite left behind: %s", ", ".join(removed)
        )
    return removed


def verify_key(path: Path | str, key: bytes) -> None:
    """Prove the key opens the file without touching it: a read-only,
    immutable open, so a wrong key neither writes nor folds the WAL (a
    writable check did both: 2.23.2 gate, NEW-109). Raises CompanyFileLocked
    with the sentence to show."""
    conn = connect_raw(Path(path), key, readonly=True)
    conn.close()


# ---------------------------------------------------------------------------
# Status and the startup guard
# ---------------------------------------------------------------------------


def status() -> dict:
    """What the Settings page shows: the engine behind the books, whether
    this build can encrypt, whether the file is encrypted now, and where a
    key would come from."""
    from app.config import DATABASE_URL
    from app.database import another_company_url

    url = another_company_url() or DATABASE_URL
    if url.startswith("postgresql"):
        engine = "postgresql"
    elif url.startswith("sqlite"):
        engine = "sqlite"
    else:
        engine = "other"
    path = sqlite_path(url) if engine == "sqlite" else None
    return {
        "engine": engine,
        "supported": engine == "sqlite" and path is not None and available(),
        "encrypted": is_encrypted_file(path),
        "key_source": key_source(),
        "file": path.name if path is not None else None,
        "cipher_version": cipher_version() if available() else None,
    }


def refuse_a_locked_file() -> None:
    """Startup guard: an encrypted company file with no key, or no SQLCipher
    in this build, stops the start with one sentence before migrations or
    create_all can touch it."""
    path = active_path()
    sweep_stale_conversions(path)
    if not is_encrypted_file(path):
        return
    if not available():
        raise RuntimeError(UNAVAILABLE_MESSAGE)
    key = resolve_key()
    if key is None:
        raise RuntimeError(NO_KEY_MESSAGE)
    try:
        verify_key(path, key)
    except CompanyFileLocked as exc:
        raise RuntimeError(str(exc)) from exc
