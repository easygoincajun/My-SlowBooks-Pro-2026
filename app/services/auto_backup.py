# ============================================================================
# Automatic backups on a schedule the administrator sets in Settings.
#
# The schedule lives in the backup settings file (backup_service): every N
# hours, daily at a time, or weekly on a day at a time, keeping the newest N
# automatic backups. A background thread started with the app checks once a
# minute. Times are this machine's local clock.
#
# Backups only run while the app is running. One that came due while it was
# closed runs within a minute of the next start. A failed one (the backup
# drive unplugged) is tried again an hour later and shown in Settings.
# ============================================================================

import logging
import os
import threading
import time
from datetime import datetime, timedelta

from app.services import backup_service

logger = logging.getLogger(__name__)

FREQUENCIES = ("off", "hours", "daily", "weekly")
DEFAULT_SCHEDULE = {
    "frequency": "off",
    "every_hours": 24,
    "time": "23:00",
    "weekday": 6,  # Sunday (Monday is 0)
    "keep": 14,
}
RETRY_AFTER = timedelta(hours=1)
CHECK_EVERY_SECONDS = 60


def get_schedule() -> dict:
    saved = backup_service.read_backup_settings().get("schedule") or {}
    return {**DEFAULT_SCHEDULE, **saved}


def set_schedule(schedule: dict, now: datetime | None = None) -> dict:
    """Save the schedule. ``since`` marks when it was set: the first backup
    comes at the first scheduled time after that, not straight away."""
    now = now or datetime.now()
    saved = {k: schedule[k] for k in DEFAULT_SCHEDULE}
    saved["since"] = now.isoformat(timespec="seconds")
    backup_service.update_backup_settings(schedule=saved)
    return get_schedule()


def last_run() -> dict | None:
    """{"at", "ok", "message"} for the last automatic backup, or None."""
    return backup_service.read_backup_settings().get("last_auto")


def _slot_on_or_before(schedule: dict, t: datetime) -> datetime:
    """The latest daily/weekly scheduled moment at or before ``t``."""
    hh, mm = map(int, schedule["time"].split(":"))
    slot = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if schedule["frequency"] == "weekly":
        slot -= timedelta(days=(slot.weekday() - schedule["weekday"]) % 7)
        return slot if slot <= t else slot - timedelta(days=7)
    return slot if slot <= t else slot - timedelta(days=1)


def next_backup_at(schedule: dict, last: dict | None) -> datetime | None:
    """When the next automatic backup is due (possibly already past), or
    None when automatic backups are off."""
    if schedule["frequency"] not in FREQUENCIES[1:]:
        return None
    since = datetime.fromisoformat(schedule["since"])
    ref = since
    if last:
        at = datetime.fromisoformat(last["at"])
        if at >= since:  # a run under an older schedule doesn't count
            if not last.get("ok"):
                return at + RETRY_AFTER
            ref = at
    if schedule["frequency"] == "hours":
        return ref + timedelta(hours=schedule["every_hours"])
    step = 7 if schedule["frequency"] == "weekly" else 1
    return _slot_on_or_before(schedule, ref) + timedelta(days=step)


# Under Docker two uvicorn workers each run this thread; the lock file makes
# sure only one of them takes a given backup. A lock older than an hour was
# left by a process that died mid-backup.
# ponytail: lock-file mutex, fine for a few workers on one machine.
def _lock_path():
    return backup_service.BACKUP_DIR / ".auto-backup.lock"


def _acquire_lock() -> bool:
    path = _lock_path()
    try:
        if time.time() - path.stat().st_mtime > 3600:
            path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except OSError:
        return False


def run_if_due(now: datetime | None = None, session_factory=None) -> dict | None:
    """Take an automatic backup when one is due. Returns what was recorded
    as the last run, or None when nothing was due."""
    now = now or datetime.now()
    schedule = get_schedule()
    due = next_backup_at(schedule, last_run())
    if due is None or now < due:
        return None
    if not _acquire_lock():
        return None
    try:
        # another worker may have taken it between the check and the lock
        due = next_backup_at(get_schedule(), last_run())
        if due is None or now < due:
            return None
        if session_factory is None:
            from app.database import SessionLocal as session_factory
        db = session_factory()
        try:
            result = backup_service.create_backup(
                db,
                notes="Automatic backup",
                backup_type="auto",
                tag=backup_service.AUTO_TAG,
            )
            if result.get("success"):
                backup_service.prune_auto_backups(db, schedule["keep"])
        finally:
            db.close()
        record = {
            "at": now.isoformat(timespec="seconds"),
            "ok": bool(result.get("success")),
            "message": result.get("filename") or result.get("error", "Backup failed"),
        }
        if not record["ok"]:
            logger.warning("Automatic backup failed: %s", record["message"])
        backup_service.update_backup_settings(last_auto=record)
        return record
    finally:
        _lock_path().unlink(missing_ok=True)


def _loop(stop: threading.Event) -> None:
    while not stop.wait(CHECK_EVERY_SECONDS):
        try:
            run_if_due()
        except Exception:
            logger.exception("Automatic backup check failed")


def start() -> threading.Event:
    """Start the background checker; set the returned event to stop it."""
    stop = threading.Event()
    threading.Thread(
        target=_loop, args=(stop,), name="auto-backup", daemon=True
    ).start()
    return stop
