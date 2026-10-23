# ============================================================================
# Company file encryption — status and the switch (issue #290).
#
# GET  /api/company-file/encryption               what Settings shows an administrator
# POST /api/company-file/encryption               turn it on or off, in place
# POST /api/company-file/encryption/recovery-key  a new recovery key (retires the old)
#
# All three are an administrator's. The switch also asks for the company's
# name, typed, the way a restore asks before it replaces the books: a
# change that rewrites every page of the company file is not one click.
#
# Two ways to hold the key. Server Edition sets COMPANY_FILE_ENCRYPTION_SECRET
# where it starts (the way PAYROLL_ENCRYPTION_SECRET is set) and the key is
# derived from it. A desktop has nobody at an environment variable: turning
# encryption on there makes a recovery key, shown once, derives the file's
# key from it, and wraps that key for the administrator's own password in
# a keyring beside the file (app/services/company_keyring.py); the
# administrator's sign-in at the picker unlocks the books from then on.
# PostgreSQL deployments are told that encryption at rest is the host's.
# ============================================================================

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.routes._roles import require_admin
from app.schemas.common import StrictModel
from app.services import company_file, company_keyring
from app.services.backup_service import _same_name, current_company_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/company-file", tags=["company-file"])

NO_KEY_SOURCE_MESSAGE = (
    "No key is set for this company file: set COMPANY_FILE_ENCRYPTION_SECRET "
    "where SlowBooks Pro starts and start it again, then turn encryption on."
)
NAME_MISMATCH_MESSAGE = (
    "Type the company's name exactly as it appears in Settings to confirm."
)
PASSWORD_NEEDED_MESSAGE = (
    "Enter your own password: the company file's key is wrapped for it, so "
    "your sign-in unlocks the books from now on."
)
WRONG_PASSWORD_MESSAGE = "That is not your password."
NO_USER_MESSAGE = (
    "Sign in with a username to turn encryption on: the key is wrapped for "
    "the administrator who turns it on."
)


class EncryptionChange(StrictModel):
    enabled: bool
    company_name: str
    # the administrator's own password, on a desktop (no operator secret):
    # the key is wrapped for it
    password: Optional[str] = Field(None, max_length=512)


class NewRecoveryKey(StrictModel):
    company_name: str
    password: str = Field(..., max_length=512)


def _status() -> dict:
    state = company_file.status()
    path = company_file.active_path()
    state["keyring"] = company_keyring.status(path) if state["encrypted"] else None
    # a desktop with no operator secret can turn encryption on with the
    # administrator's own password; a secret-keyed install as before
    state["can_turn_on"] = bool(
        state["supported"]
        and not state["encrypted"]
        and (state["key_source"] in ("secret", "launcher") or _desktop())
    )
    state["desktop_key"] = _desktop() and state["key_source"] != "secret"
    return state


def _desktop() -> bool:
    """The desktop's own key is the company the window (or the server) was
    started for: another company a Server Edition server serves is keyed by
    the operator secret only (#358)."""
    import os

    return (
        os.environ.get("SLOWBOOKS_DESKTOP") == "1"
        and not company_file.serving_another_company()
    )


def _signed_in_admin(request: Request, db: Session, password: str | None):
    """The administrator making the change, their password proven; or the
    sentence why not."""
    from app.models.users import User
    from app.services.auth import verify_password

    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=400, detail=NO_USER_MESSAGE)
    if not password:
        raise HTTPException(status_code=400, detail=PASSWORD_NEEDED_MESSAGE)
    if not verify_password(password, user.password_hash):
        raise HTTPException(status_code=403, detail=WRONG_PASSWORD_MESSAGE)
    return user


def _rewrite(db: Session, path, work) -> None:
    """Run a rewrite of the company file with the pool let go: this
    request's session and the pool's idle connections are released first
    (on Windows an open handle refuses the replace), and every connection
    after must key itself by what is now on disk."""
    import app.database as db_module

    db.close()
    db_module.current_engine().dispose()
    try:
        work()
    except company_file.CompanyFileLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        # Never the exception text: it names paths (py/stack-trace-exposure).
        logger.exception("company file rewrite failed")
        raise HTTPException(
            status_code=500,
            detail="The company file could not be converted; the books are "
            "as they were. Check the log.",
        )
    finally:
        db_module.current_engine().dispose()


@router.get("/encryption")
def encryption_status(request: Request):
    require_admin(request)
    return _status()


def turn_on_with_recovery_key(
    db: Session, path, *, username: str, password: str, company_name: str
) -> str:
    """Encrypt the company file under a new recovery key, wrapped for the
    administrator; the process keeps the key. Returns the recovery key, the
    one time it is shown. Shared with first-run setup (#290)."""
    recovery = company_keyring.new_recovery_key()
    key = company_keyring.key_from_recovery(recovery)
    ring = company_keyring.new_ring(key, company_name=company_name or "")
    company_keyring.set_admin(ring, username, password, key)

    def work():
        company_file.convert(path, key, encrypt=True)
        company_keyring.save(path, ring)
        if not company_file.serving_another_company():
            company_file.set_runtime_key(key)

    _rewrite(db, path, work)
    return recovery


@router.post("/encryption")
def change_encryption(
    data: EncryptionChange, request: Request, db: Session = Depends(get_db)
):
    require_admin(request)
    state = company_file.status()
    if state["engine"] == "postgresql":
        raise HTTPException(status_code=409, detail=company_file.POSTGRESQL_MESSAGE)
    if not state["supported"]:
        raise HTTPException(status_code=409, detail=company_file.UNAVAILABLE_MESSAGE)
    if not _same_name(data.company_name, current_company_name(db)):
        raise HTTPException(status_code=400, detail=NAME_MISMATCH_MESSAGE)
    if data.enabled == state["encrypted"]:
        return {**_status(), "changed": False}
    path = company_file.active_path()
    who = request.session.get("username") or "operator"

    if data.enabled and state["key_source"] is None:
        # the desktop's own key (#290): a recovery key, shown once, and the
        # administrator's password wrapping it
        if not _desktop():
            raise HTTPException(status_code=409, detail=NO_KEY_SOURCE_MESSAGE)
        user = _signed_in_admin(request, db, data.password)
        company = current_company_name(db)
        recovery = turn_on_with_recovery_key(
            db,
            path,
            username=user.username,
            password=data.password,
            company_name=company,
        )
        logger.info("company file encryption turned on by %s (recovery key made)", who)
        return {
            **_status(),
            "changed": True,
            "recovery_key": recovery,
            "recovery_file": company_keyring.recovery_file_text(company, recovery),
        }

    key = company_file.resolve_key()
    if key is None:
        raise HTTPException(status_code=409, detail=NO_KEY_SOURCE_MESSAGE)
    if not data.enabled and request.session.get("user_id"):
        # the same proof Turn on and New recovery key ask for (2.24.0 gate,
        # the agents' question on round 1): the administrator's own password
        _signed_in_admin(request, db, data.password)

    def work():
        company_file.convert(path, key, encrypt=data.enabled)
        if not data.enabled:
            # plain again: the keyring has nothing to hold, and the process
            # keeps no key for a plain file
            company_keyring.remove(path)
            if not company_file.serving_another_company():
                company_file.set_runtime_key(None)

    _rewrite(db, path, work)
    logger.info(
        "company file encryption turned %s by %s",
        "on" if data.enabled else "off",
        who,
    )
    return {**_status(), "changed": True}


@router.post("/encryption/recovery-key")
def new_recovery_key(
    data: NewRecoveryKey, request: Request, db: Session = Depends(get_db)
):
    """A new recovery key: the file is re-keyed in place under the key it
    derives, the administrator asking is wrapped for it now, every other
    administrator at their next sign-in; the old recovery key opens
    nothing from here on."""
    require_admin(request)
    state = company_file.status()
    if not state["encrypted"] or state["key_source"] != "keyring":
        raise HTTPException(
            status_code=409,
            detail=(
                "This company file has no recovery key: it is "
                + (
                    "keyed by COMPANY_FILE_ENCRYPTION_SECRET."
                    if state["key_source"] == "secret"
                    else "not encrypted."
                )
            ),
        )
    if not _same_name(data.company_name, current_company_name(db)):
        raise HTTPException(status_code=400, detail=NAME_MISMATCH_MESSAGE)
    user = _signed_in_admin(request, db, data.password)
    path = company_file.active_path()
    old_key = company_file.resolve_key()
    if old_key is None:
        raise HTTPException(status_code=409, detail=company_file.NO_KEY_MESSAGE)
    company = current_company_name(db)
    recovery = company_keyring.new_recovery_key()
    new_key = company_keyring.key_from_recovery(recovery)
    ring = company_keyring.load(path) or company_keyring.new_ring(new_key)
    ring["key_id"] = company_keyring.new_ring(new_key)["key_id"]
    ring["recovery_made_at"] = company_keyring.new_ring(new_key)["recovery_made_at"]
    ring["company_name"] = company or ring.get("company_name", "")
    company_keyring.set_admin(ring, user.username, data.password, new_key)

    def work():
        company_file.rekey(path, old_key, new_key)
        company_keyring.save(path, ring)
        company_file.set_runtime_key(new_key)

    _rewrite(db, path, work)
    logger.info("a new recovery key was made by %s", user.username)
    return {
        **_status(),
        "changed": True,
        "recovery_key": recovery,
        "recovery_file": company_keyring.recovery_file_text(company, recovery),
    }
