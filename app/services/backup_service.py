# ============================================================================
# Backup/Restore Service — accessible from settings
# Feature 11: Database backup and restore
#
# PostgreSQL (Docker / server installs): pg_dump/pg_restore subprocess.
# SQLite (native desktop installs): a snapshot copy of the company's .db
# file via sqlite3's online backup API (consistent even with the app's own
# connections open). An encrypted company file (#290) is copied under its
# key, so its backups are ciphertext too; a restore leaves the books in the
# state they are in, whichever state the backup was in.
# ============================================================================

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import DATABASE_URL
from app.models.backups import Backup
from app.services import company_file, storage

logger = logging.getLogger(__name__)

BACKUP_DIR = storage.backups_root()
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# Strict filename allow-list. Backup files we create are named
# "<company>_YYYYMMDD_HHMMSS.db" (SQLite) or ".sql" (Postgres) — before
# 2.17.4 every company's were "slowbooks_YYYYMMDD_HHMMSS", which still
# list and restore. We accept any safe basename matching this character
# class with a known backup extension. NO path separators, NO ".."
# components -- this is the trust boundary that CodeQL needs to see at the
# start of backup_path() before any folder / filename is constructed.
# Matched with re.fullmatch (not .match + ^...$): in Python `$` also matches
# just before a trailing "\n", so "evil.db\n" would slip through .match.
# The extension is split off first and the stem matched on its own: one
# pattern holding both a "." in the class and a literal "\." before the
# extension backtracks polynomially on a long run of those characters.
_BACKUP_STEM_RE = re.compile(r"[A-Za-z0-9_.-]+")
_BACKUP_EXTENSIONS = frozenset({"sql", "dump", "backup", "db"})

# Every company's backups share one folder, and the old names said nothing
# about whose they were (explore 2.17.3, macbase1 F25: Riverbend x3,
# NEONpulse x2 and Harbor Light x1, all "slowbooks_<date>_<time>.db"). The
# name now starts with the company's own part: its file name on the
# desktop, its database name on a server.
_NAME_RE = re.compile(
    r"^(?P<slug>.+)_(?P<stamp>\d{8}_\d{6})(?:-(?P<tag>[a-z0-9-]+))?"
    r"\.(?P<ext>sql|dump|backup|db)$"
)
LEGACY_SLUG = "slowbooks"
# The copy taken automatically just before a restore replaces the books.
PRE_RESTORE_TAG = "before-restore"
# Automatic backups (app/services/auto_backup.py): "-auto" in the name, or
# "-auto-2" when two land in one second. Only these are ever cleaned up.
AUTO_TAG = "auto"
_AUTO_TAG_RE = re.compile(r"^auto(-\d+)?$")


def is_auto_backup(filename: str) -> bool:
    parsed = parse_backup_name(filename)
    return bool(parsed and _AUTO_TAG_RE.match(parsed["tag"] or ""))


# ---------------------------------------------------------------------------
# Where backups go. BACKUP_DIR is the default; an administrator can choose
# another folder in Settings (an external drive, iCloud Drive). The choice is
# kept in BACKUP_DIR/backup-settings.json with the automatic-backup schedule
# (app/services/auto_backup.py): machine settings rather than company ones,
# so restoring a backup must not move the folder its safety copy went to.
# Backups already in the default folder stay listed and restorable.
# ---------------------------------------------------------------------------


def _settings_file() -> Path:
    return BACKUP_DIR / "backup-settings.json"


def read_backup_settings() -> dict:
    try:
        settings = json.loads(_settings_file().read_text())
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def update_backup_settings(**changes) -> dict:
    """Merge ``changes`` into the settings file; a None value removes a key."""
    settings = read_backup_settings()
    settings.update(changes)
    settings = {k: v for k, v in settings.items() if v is not None}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _settings_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2))
    tmp.replace(_settings_file())  # never a half-written file
    return settings


def custom_backup_dir() -> Path | None:
    """The folder chosen in Settings, or None for the default."""
    folder = read_backup_settings().get("folder")
    return Path(folder) if isinstance(folder, str) and folder else None


def backup_dir() -> Path:
    """Where new backups are written."""
    return custom_backup_dir() or BACKUP_DIR


def _backup_dirs() -> list[Path]:
    """Every folder backups are listed and restored from, chosen one first."""
    dirs = [backup_dir()]
    if dirs[0] != BACKUP_DIR:
        dirs.append(BACKUP_DIR)
    return [d for d in dirs if d.is_dir()]


def set_backup_dir(folder: str | None) -> Path:
    """Choose where new backups go; blank goes back to the default.
    Raises ValueError with a sentence for the person when the folder can't
    be used. Never creates the folder: a path on a drive that isn't plugged
    in would otherwise quietly become a folder on the startup disk."""
    folder = (folder or "").strip()
    if not folder:
        update_backup_settings(folder=None)
        return BACKUP_DIR
    path = Path(folder).expanduser()
    if not path.is_absolute():
        raise ValueError(
            "Enter the folder's full path, e.g. /Volumes/Backup/SlowBooks."
        )
    if not path.is_dir():
        raise ValueError(f"There is no folder at {path}.")
    try:
        with tempfile.NamedTemporaryFile(dir=path):
            pass
    except OSError:
        raise ValueError(f"SlowBooks Pro can't save files in {path}.")
    path = path.resolve()
    update_backup_settings(folder=str(path))
    return path


def _unavailable(folder: Path) -> dict:
    return {
        "success": False,
        "error": (
            f"The backup folder {folder} is not available. Connect the drive "
            "it is on, or choose another folder in Settings."
        ),
    }


def parse_backup_name(filename: str) -> dict | None:
    """{"slug", "stamp", "tag"} for a name this app makes, else None."""
    m = _NAME_RE.match(filename or "")
    return m.groupdict() if m else None


def company_slug() -> str:
    """This company's part of a backup file name: "harbor-light-bakery" for
    harbor-light-bakery.db, the database name on a server. Never the old
    anonymous "slowbooks", so a new name can always be told from an old one."""
    if _is_sqlite():
        path = _sqlite_db_path()
        stem = path.stem if path is not None else ""
    else:
        stem = _parse_db_url(DATABASE_URL)["dbname"]
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60].strip("-")
    if not slug:
        slug = "company"
    return f"{slug}-books" if slug == LEGACY_SLUG else slug


def _new_backup_filename(tag: str | None = None) -> str:
    ext = "db" if _is_sqlite() else "sql"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{company_slug()}_{stamp}" + (f"-{tag}" if tag else "")
    name = f"{base}.{ext}"
    n = 2
    # two in one second must not overwrite, nor share a name across folders
    while backup_path(name).exists():
        name = f"{base}-{n}.{ext}"
        n += 1
    return name


def _safe_backup_filename(filename: str) -> str | None:
    """Return a safe basename for a backup file, or None if invalid.

    Uses os.path.basename() to strip any path components — this is a
    sanitizer recognized by static analyzers (CodeQL py/path-injection)
    so the returned value is treated as path-safe at downstream sinks.
    Then enforces the strict regex / length / leading-dot rules.
    """
    if not filename or len(filename) > 255:
        return None
    # os.path.basename strips any directory component; if the input
    # contained separators, the result will differ from the input and we
    # reject it (keeps "files only" semantics rather than silently
    # accepting "evil/foo.sql" as "foo.sql").
    base = os.path.basename(filename)
    if base != filename:
        return None
    if base.startswith(".") or ".." in base:
        return None
    stem, dot, extension = base.rpartition(".")
    if not dot or extension not in _BACKUP_EXTENSIONS:
        return None
    if not _BACKUP_STEM_RE.fullmatch(stem):
        return None
    return base


def _is_sqlite() -> bool:
    return DATABASE_URL.startswith("sqlite")


def _sqlite_db_path() -> Path | None:
    """Filesystem path of the active SQLite database, or None when the URL
    isn't a file-backed SQLite database (e.g. sqlite:///:memory:). On a
    Server Edition server, the company this request works in (#358)."""
    from app.database import another_company_url

    url = another_company_url() or DATABASE_URL
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///") :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def _parse_db_url(url: str) -> dict:
    """Parse PostgreSQL connection URL into components."""
    # postgresql://user:pass@host:port/dbname
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "bookkeeper",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/") or "bookkeeper",
    }


def _create_sqlite_backup(
    db: Session, notes: str, backup_type: str, tag: str | None = None
) -> dict:
    """Snapshot the active company's .db file into the backup folder."""
    src = _sqlite_db_path()
    if src is None or not src.exists():
        return {
            "success": False,
            "error": "Active database is not a file-backed SQLite database",
        }

    folder = backup_dir()
    if not folder.is_dir():
        return _unavailable(folder)
    filename = _new_backup_filename(tag)
    filepath = folder / filename

    try:
        # sqlite3's online backup API gives a consistent snapshot even if
        # the app holds open connections (unlike a raw file copy). An
        # encrypted company is copied under its key: the backup is
        # ciphertext, readable with the same key.
        key = (
            company_file.resolve_key() if company_file.is_encrypted_file(src) else None
        )
        with closing(company_file.connect_raw(src, key)) as source, closing(
            company_file.connect_raw(filepath, key)
        ) as dest:
            source.backup(dest)
    except company_file.errors():
        # Exception text can leak paths/internals into the HTTP response
        # (routes surface this error string) — log it, say something generic.
        logger.exception("SQLite backup failed")
        filepath.unlink(missing_ok=True)
        return {"success": False, "error": "SQLite backup failed. Check logs."}

    file_size = filepath.stat().st_size
    backup = Backup(
        filename=filename,
        file_size=file_size,
        backup_type=backup_type,
        notes=notes,
    )
    db.add(backup)
    db.commit()

    return {"success": True, "filename": filename, "file_size": file_size}


OTHER_KEY_MESSAGE = (
    "This backup was made under another key (another recovery key, or "
    "another install's secret). Enter the recovery key it was made under "
    "to restore it."
)


def is_encrypted_backup(path: Path | str | None) -> bool:
    """Whether a backup is an encrypted company file. Only a SQLite backup
    (.db) can be one: a Postgres backup is pg_dump's own format, which is
    not SQLite either but is not encrypted, and restoring it needs no key."""
    if path is None or Path(path).suffix != ".db":
        return False
    return company_file.is_encrypted_file(path)


def backup_key_problem(filename: str, recovery_key: str | None = None) -> dict | None:
    """Whether a backup can be opened here: None when it can (plain, or
    encrypted under this company's key, or under the recovery key given),
    else {"code", "message"}. Asked before anything is written."""
    from app.services import company_keyring

    path = backup_path(filename)
    if not is_encrypted_backup(path):
        return None
    key = company_file.resolve_key()
    if key is not None:
        try:
            company_file.verify_key(path, key)
            return None
        except company_file.CompanyFileLocked:
            pass
    if (recovery_key or "").strip():
        try:
            company_keyring.unlock_with_recovery(path, recovery_key)
            return None
        except company_file.CompanyFileLocked:
            return {
                "code": "wrong_recovery_key",
                "message": company_keyring.WRONG_RECOVERY_MESSAGE,
            }
    return {"code": "other_key", "message": OTHER_KEY_MESSAGE}


def _restore_sqlite_backup(
    filepath: Path, safe_name: str, recovery_key: str | None = None
) -> dict:
    """Copy a backup .db over the active company's database file. A backup
    encrypted under another key is opened with the recovery key it was made
    under (#290) and re-keyed, on a staged copy, to this company's key."""
    dest = _sqlite_db_path()
    if dest is None:
        return {
            "success": False,
            "error": "Active database is not a file-backed SQLite database",
        }

    # Close the app's pooled connections first so the live file can be
    # rewritten (SQLite locking; also matters for Windows file semantics).
    import app.database as db_module

    db_module.current_engine().dispose()

    live_encrypted = company_file.is_encrypted_file(dest)
    backup_encrypted = company_file.is_encrypted_file(filepath)
    key = None
    if live_encrypted or backup_encrypted:
        key = company_file.resolve_key()
        if key is None and live_encrypted:
            return {"success": False, "error": company_file.NO_KEY_MESSAGE}
    staged = None
    try:
        source_path = filepath
        if backup_encrypted:
            problem = backup_key_problem(filepath.name, recovery_key)
            if problem is not None:
                return {"success": False, "error": problem["message"], **problem}
            opens_here = key is not None
            if opens_here:
                try:
                    company_file.verify_key(filepath, key)
                except company_file.CompanyFileLocked:
                    opens_here = False
            if not opens_here:
                # another key: a staged copy is brought to this company's
                # key (or made plain, for a plain company); the backup file
                # itself is never changed
                from app.services import company_keyring

                other = company_keyring.key_from_recovery(recovery_key or "")
                staged = BACKUP_DIR / (filepath.name + ".restoring")
                shutil.copy2(filepath, staged)
                if live_encrypted:
                    company_file.rekey(staged, other, key)
                else:
                    company_file.convert(staged, other, encrypt=False)
                source_path = staged
                backup_encrypted = live_encrypted
        if backup_encrypted != live_encrypted:
            if key is None:
                return {"success": False, "error": company_file.NO_KEY_MESSAGE}
            # The books stay in the state they are in: a plain backup
            # restored into an encrypted company arrives encrypted, and
            # the other way around. The backup file itself is never
            # changed — a staged copy is converted and restored from.
            staged = BACKUP_DIR / (filepath.name + ".restoring")
            shutil.copy2(filepath, staged)
            company_file.convert(staged, key, encrypt=live_encrypted)
            source_path = staged
        live_key = key if live_encrypted else None
        with closing(
            company_file.connect_raw(source_path, live_key)
        ) as source, closing(company_file.connect_raw(dest, live_key)) as target:
            source.backup(target)
    except company_file.errors() + (OSError,):
        # Same shape as _create_sqlite_backup: no exception text in the
        # user-facing error (py/stack-trace-exposure) — details go to logs.
        logger.exception("SQLite restore failed")
        return {"success": False, "error": "SQLite restore failed. Check logs."}
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)

    return {"success": True, "message": f"Restored from {safe_name}"}


def create_backup(
    db: Session, notes: str = None, backup_type: str = "manual", tag: str = None
) -> dict:
    """Create a database backup (pg_dump on Postgres, file snapshot on SQLite),
    named for this company; ``tag`` marks it in the name (PRE_RESTORE_TAG)."""
    if _is_sqlite():
        return _create_sqlite_backup(db, notes, backup_type, tag)

    folder = backup_dir()
    if not folder.is_dir():
        return _unavailable(folder)
    params = _parse_db_url(DATABASE_URL)
    filename = _new_backup_filename(tag)
    filepath = folder / filename

    env = {"PGPASSWORD": params["password"]}

    try:
        result = subprocess.run(
            [
                "pg_dump",
                "-h",
                params["host"],
                "-p",
                params["port"],
                "-U",
                params["user"],
                "-F",
                "c",
                "-f",
                str(filepath),
                params["dbname"],
            ],
            env={**dict(__import__("os").environ), **env},
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        file_size = filepath.stat().st_size

        backup = Backup(
            filename=filename,
            file_size=file_size,
            backup_type=backup_type,
            notes=notes,
        )
        db.add(backup)
        db.commit()

        return {"success": True, "filename": filename, "file_size": file_size}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Backup timed out"}
    except FileNotFoundError:
        return {
            "success": False,
            "error": "pg_dump not found. Is PostgreSQL client installed?",
        }


def restore_backup(db: Session, filename: str, recovery_key: str | None = None) -> dict:
    """Restore a database from a backup file."""
    # Trust boundary: validate filename as a safe basename BEFORE using it
    # to construct any filesystem path. is_relative_to is the
    # belt-and-suspenders check after.
    filepath = backup_path(filename)
    if filepath is None:
        return {"success": False, "error": "Invalid filename"}
    safe_name = filepath.name
    if not filepath.exists():
        return {"success": False, "error": f"Backup file not found: {safe_name}"}

    if _is_sqlite():
        return _restore_sqlite_backup(filepath, safe_name, recovery_key)

    params = _parse_db_url(DATABASE_URL)
    env = {"PGPASSWORD": params["password"]}

    try:
        result = subprocess.run(
            [
                "pg_restore",
                "-h",
                params["host"],
                "-p",
                params["port"],
                "-U",
                params["user"],
                "-d",
                params["dbname"],
                "--clean",
                "--if-exists",
                str(filepath),
            ],
            env={**dict(__import__("os").environ), **env},
            capture_output=True,
            text=True,
            timeout=300,
        )
        # pg_restore may return non-zero even on partial success
        if result.returncode != 0 and "error" in result.stderr.lower():
            return {"success": False, "error": result.stderr[:500]}

        return {"success": True, "message": f"Restored from {filename}"}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Restore timed out"}
    except FileNotFoundError:
        return {"success": False, "error": "pg_restore not found"}


# ---------------------------------------------------------------------------
# A backup chosen on the person's computer (a thumb drive, a download).
# It is copied into the backup folder and then restored the ordinary way,
# so it gets every check a listed backup gets: whose books, which version,
# and a safety copy of the books as they are first.
# ---------------------------------------------------------------------------

_SQLITE_MAGIC = b"SQLite format 3\x00"
_PG_DUMP_MAGIC = b"PGDMP"  # pg_dump -F c, what create_backup() writes


def _uploaded_name(original: str | None, ext: str) -> str:
    """A safe, unused backup name kept close to the file's own: its name
    says whose books it is (parse_backup_name)."""
    stem = Path(os.path.basename(original or "")).stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:100] or "uploaded"
    name, n = f"{stem}.{ext}", 2
    while backup_path(name).exists():
        name = f"{stem}-{n}.{ext}"
        n += 1
    return name


def save_uploaded_backup(fileobj, original_name: str | None) -> dict:
    """Copy an uploaded backup into the backup folder.
    {"success": True, "filename"} or {"success": False, "error"}."""
    folder = backup_dir()
    if not folder.is_dir():
        return _unavailable(folder)
    magic = _SQLITE_MAGIC if _is_sqlite() else _PG_DUMP_MAGIC
    head = fileobj.read(len(magic))
    fileobj.seek(0)
    # An encrypted company file's backup (#290) has no SQLite header. It is
    # taken only by an encrypted company, and kept only if it opens with
    # that company's key as a company file (checked below).
    encrypted_books = _is_sqlite() and company_file.is_encrypted_file(
        company_file.active_path()
    )
    if head != magic and not encrypted_books:
        return {
            "success": False,
            "error": "This is not a SlowBooks Pro backup for this kind of install.",
        }
    filename = _uploaded_name(original_name, "db" if _is_sqlite() else "sql")
    path = folder / filename
    with open(path, "xb") as out:
        shutil.copyfileobj(fileobj, out)
    if head != magic and backup_key_problem(filename) is not None:
        path.unlink(missing_ok=True)
        return {
            "success": False,
            "error": "This file is not a backup this company's key opens. A backup "
            "of a company encrypted under another key is restored from that "
            "company's own backup list.",
        }
    # A SQLite file that isn't a company file (no migration history) would
    # replace the books with something the app can't open.
    if _is_sqlite() and not read_backup_facts(filename).get("revision"):
        path.unlink(missing_ok=True)
        return {
            "success": False,
            "error": "This file is a database, but not a SlowBooks Pro company backup.",
        }
    return {"success": True, "filename": filename}


def list_backup_files() -> list[dict]:
    """List every backup file in the (shared) backup folders, any company."""
    files = []
    candidates = _backup_files()
    for f in sorted(candidates, key=lambda p: p.name, reverse=True):
        files.append(
            {
                "filename": f.name,
                "file_size": f.stat().st_size,
                "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                # a backup made before encryption was turned on is plain
                # SQLite (2.24.0 gate, NEW-158): the page says which
                "encrypted": is_encrypted_backup(f),
            }
        )
    return files


# ---------------------------------------------------------------------------
# Whose backup is it? (explore 2.17.3, macbase1 F25)
# ---------------------------------------------------------------------------


def backup_path(filename: str) -> Path | None:
    """The backup's path in the first backup folder that has it (the chosen
    folder, then the default), else where it would be in the chosen one;
    None for a name that is not a backup file. The trust boundary for every
    read: a safe basename, then contained in a backup folder."""
    safe_name = _safe_backup_filename(filename)
    if safe_name is None:
        return None
    first = None
    for folder in [backup_dir()] + [d for d in _backup_dirs() if d != backup_dir()]:
        # normpath + startswith is the containment guard CodeQL recognizes
        # for py/path-injection (is_relative_to on Path objects is not).
        backup_root = os.path.normpath(str(folder))
        candidate = os.path.normpath(os.path.join(backup_root, safe_name))
        if not candidate.startswith(backup_root + os.sep):
            return None
        if os.path.exists(candidate):
            return Path(candidate)
        first = first or Path(candidate)
    return first


def _backup_files() -> list[Path]:
    """Every backup file in the backup folders; a name in two folders is
    the chosen folder's."""
    seen, found = set(), []
    for folder in _backup_dirs():
        for p in folder.iterdir():
            if p.name in seen or not p.is_file():
                continue
            if _safe_backup_filename(p.name) is None:
                continue
            seen.add(p.name)
            found.append(p)
    return found


def read_backup_facts(filename: str) -> dict:
    """What a SQLite backup says about itself: {"company_name", "revision"}
    (either may be None). Opened read-only and immutable, so reading never
    changes the file. Empty for a Postgres dump or anything unreadable."""
    path = backup_path(filename)
    if path is None or path.suffix != ".db" or not path.exists():
        return {}
    facts = {"company_name": None, "revision": None}
    key = None
    if company_file.is_encrypted_file(path):
        key = company_file.resolve_key()
        if key is None:
            return {}  # an encrypted backup with no key in hand: unreadable here
    try:
        with closing(company_file.connect_raw(path, key, readonly=True)) as conn:
            for fact, sql in (
                (
                    "company_name",
                    "SELECT value FROM settings WHERE key = 'company_name'",
                ),
                ("revision", "SELECT version_num FROM alembic_version"),
            ):
                try:
                    row = conn.execute(sql).fetchone()
                except company_file.errors():
                    row = None
                facts[fact] = (row[0] or None) if row else None
    except company_file.errors():
        logger.exception("Could not read backup %s", path.name)
        return {}
    return facts


def _same_name(a: str | None, b: str | None) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def current_company_name(db: Session) -> str:
    from app.models.settings import DEFAULT_SETTINGS
    from app.services.settings_service import get_setting_raw

    return (get_setting_raw(db, "company_name") or "").strip() or DEFAULT_SETTINGS[
        "company_name"
    ]


def list_company_backups(db: Session) -> list[dict]:
    """The open company's backups, newest first.

    The folder holds every company's. This company's are the files named
    for it, plus older anonymous "slowbooks_*" files that are its own: ones
    its backups table lists, or (a SQLite copy) that carry its company name.
    Read from the folder, not only the table: restoring an older backup
    replaces the table too, and the safety copy taken just before it must
    still be listed afterwards."""
    slug = company_slug()
    rows = {b.filename: b for b in db.query(Backup).all()}
    name = current_company_name(db)
    listed = []
    for path in _backup_files():
        filename = path.name
        parsed = parse_backup_name(filename)
        owner = parsed["slug"] if parsed else None
        if owner == slug:
            mine = True
        elif owner not in (None, LEGACY_SLUG):
            mine = False  # named for another company
        elif filename in rows:
            mine = True
        else:
            mine = filename.endswith(".db") and _same_name(
                read_backup_facts(filename).get("company_name"), name
            )
        if not mine:
            continue
        row = rows.get(filename)
        stat = path.stat()
        pre_restore = bool(parsed and (parsed["tag"] or "").startswith(PRE_RESTORE_TAG))
        if row is not None and row.created_at is not None:
            stamped = row.created_at
            if stamped.tzinfo is None:  # SQLite's CURRENT_TIMESTAMP is UTC
                stamped = stamped.replace(tzinfo=timezone.utc)
            created = stamped.isoformat()
        else:
            created = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        listed.append(
            {
                "id": row.id if row is not None else None,
                # a backup made before encryption was turned on is plain
                # SQLite (2.24.0 gate, NEW-158 / W-25); the page says which
                "encrypted": is_encrypted_backup(path),
                "filename": filename,
                "file_size": stat.st_size,
                "backup_type": (
                    row.backup_type
                    if row is not None
                    else (
                        "pre-restore"
                        if pre_restore
                        else "auto" if is_auto_backup(filename) else "manual"
                    )
                ),
                "notes": (
                    row.notes
                    if row is not None
                    else (
                        "Taken automatically just before a restore"
                        if pre_restore
                        else None
                    )
                ),
                "created_at": created,
                "_sort": (parsed["stamp"] if parsed else "")
                or datetime.fromtimestamp(stat.st_mtime).strftime("%Y%m%d_%H%M%S"),
            }
        )
    listed.sort(key=lambda b: (b["_sort"], b["filename"]), reverse=True)
    for b in listed:
        del b["_sort"]
    return listed


def prune_auto_backups(db: Session, keep: int) -> list[str]:
    """Delete this company's automatic backups beyond the newest ``keep``.
    Manual and pre-restore backups are never touched. Returns the names."""
    autos = [
        b["filename"] for b in list_company_backups(db) if is_auto_backup(b["filename"])
    ]
    removed = autos[keep:]
    for name in removed:
        path = backup_path(name)
        if path is not None:
            path.unlink(missing_ok=True)
    if removed:
        db.query(Backup).filter(Backup.filename.in_(removed)).delete(
            synchronize_session=False
        )
        db.commit()
    return removed


def other_company(db: Session, filename: str) -> dict | None:
    """None when the backup is this company's (or nothing says otherwise);
    else {"backup_company", "current_company"} naming both. A backup named
    for another company, or carrying another company name in its settings,
    belongs to that company."""
    current = current_company_name(db)
    parsed = parse_backup_name(filename)
    stored = read_backup_facts(filename).get("company_name")
    if stored and not _same_name(stored, current):
        return {"backup_company": stored, "current_company": current}
    if parsed and parsed["slug"] not in (company_slug(), LEGACY_SLUG):
        return {"backup_company": stored or parsed["slug"], "current_company": current}
    return None


def _migration_script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg, ScriptDirectory.from_config(cfg)


def backup_revision_problem(filename: str) -> str | None:
    """A sentence when this build cannot open the backup's books: a copy
    made by a newer SlowBooks Pro, whose schema this one does not know.
    Restoring it would leave a company file the app refuses to open."""
    revision = read_backup_facts(filename).get("revision")
    if not revision:
        return None
    try:
        _cfg, script = _migration_script()
        known = {rev.revision for rev in script.walk_revisions()}
    except Exception:
        logger.exception("Could not read the migration history")
        return None
    if revision in known:
        return None
    return (
        "This backup was made by a newer version of SlowBooks Pro than this "
        "one, which cannot open it. Update SlowBooks Pro, then restore it."
    )


def bring_restored_books_up_to_date() -> dict:
    """After a SQLite restore: an older backup is brought to this build's
    schema, the way the desktop app does when it opens a company, so the
    running app can use it straight away. {"success": bool, "error"}."""
    if not _is_sqlite():
        return {"success": True}
    path = _sqlite_db_path()
    if path is None:
        return {"success": True}
    key = company_file.resolve_key() if company_file.is_encrypted_file(path) else None
    try:
        with closing(company_file.connect_raw(path, key)) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        current = row[0] if row else None
    except company_file.errors():
        current = None  # no migration history: not a file alembic manages
    if not current:
        return {"success": True}
    try:
        cfg, script = _migration_script()
        if current == script.get_current_head():
            return {"success": True}
        from alembic import command

        cfg.attributes["database_url"] = "sqlite:///" + path.as_posix()
        cfg.attributes["company_file_key"] = key
        command.upgrade(cfg, "head")
    except Exception:
        logger.exception("Could not upgrade the restored books")
        return {
            "success": False,
            "error": "The restored books could not be brought up to this version.",
        }
    import app.database as db_module

    db_module.current_engine().dispose()
    return {"success": True}
