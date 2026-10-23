"""SlowBooks' own Accountant's Copy (#346): send, open, send back, apply."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.routes._roles import require_admin
from app.schemas.common import StrictModel
from app.services import accountants_copy as svc
from app.services.upload_limits import read_limited

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accountants-copy", tags=["accountants-copy"])

# the copies made this session, kept until downloaded (the passphrase is
# in the answer that made them, not here)
_made: dict[str, tuple[str, bytes]] = {}


class MakeCopy(StrictModel):
    dividing_date: date
    # the company's name, typed, as the encryption switch asks (#290)
    company_name: str


def _same_name(a: str, b: str) -> bool:
    return " ".join((a or "").split()).lower() == " ".join((b or "").split()).lower()


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """Whether this company is an accountant's copy, and the copies these
    books have sent out."""
    from app.models.accountants_copies import AccountantsCopy

    m = svc.mode_of(db)
    sent = [
        {
            "copy_id": c.copy_id,
            "company_name": c.company_name,
            "dividing_date": c.dividing_date.isoformat(),
            "made_at": c.made_at.isoformat() if c.made_at else None,
            "applied": len(c.applied or []),
        }
        for c in db.query(AccountantsCopy)
        .order_by(AccountantsCopy.made_at.desc())
        .all()
    ]
    return {
        "is_copy": bool(m),
        "copy": {k: v for k, v in (m or {}).items() if k != "token"} or None,
        "sent": sent,
        "can_make": svc.company_file.active_path() is not None
        and svc.company_file.available(),
    }


@router.post("/make", status_code=201)
def make(data: MakeCopy, request: Request, db: Session = Depends(get_db)):
    """Send to accountant: the copy is made and held for download; the
    passphrase comes back once, here."""
    require_admin(request)
    from app.services.backup_service import current_company_name

    if svc.mode_of(db):
        raise HTTPException(
            status_code=409, detail="This company is itself an accountant's copy."
        )
    if not _same_name(data.company_name, current_company_name(db) or ""):
        raise HTTPException(
            status_code=400,
            detail="Type the company's name exactly as it appears in Settings to confirm.",
        )
    content, passphrase, meta = svc.make_copy(db, data.dividing_date)
    name = svc.copy_file_name(meta["company_name"], data.dividing_date)
    _made[meta["copy_id"]] = (name, content)
    logger.info(
        "accountant's copy %s made by %s",
        meta["copy_id"][:8],
        request.session.get("username") or "operator",
    )
    return {**meta, "passphrase": passphrase, "file_name": name, "size": len(content)}


@router.get("/download/{copy_id}")
def download(copy_id: str, request: Request):
    require_admin(request)
    found = _made.get(copy_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail="That copy is not here to download any more; make it again.",
        )
    name, content = found
    from app.routes.csv import _DESKTOP_HEADER

    disposition = "inline" if request.headers.get(_DESKTOP_HEADER) else "attachment"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f"{disposition}; filename={name}"},
    )


@router.post("/open", status_code=201)
async def open_copy(
    request: Request,
    file: UploadFile = File(...),
    passphrase: str = Form(...),
    db: Session = Depends(get_db),
):
    """Open accountant's copy: the file and its passphrase become a company
    file of this install's own, listed on the picker."""
    require_admin(request)
    content = await read_limited(file, label="accountant's copy")
    local_key = None
    active = svc.company_file.active_path()
    if active is not None and svc.company_file.is_encrypted_file(active):
        local_key = svc.company_file.resolve_key()
    out = svc.open_copy(content, passphrase, local_key=local_key)
    return out


@router.get("/changes")
def changes(request: Request, db: Session = Depends(get_db)):
    """Send changes back: the entries posted in this copy, signed, as a
    file to send."""
    who = request.session.get("display_name") or request.session.get("username") or ""
    body = svc.changes(db, accountant=who)
    import json

    from app.routes.csv import _DESKTOP_HEADER

    name = svc.changes_file_name(body)
    disposition = "inline" if request.headers.get(_DESKTOP_HEADER) else "attachment"
    return Response(
        content=json.dumps(body, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"{disposition}; filename={name}"},
    )


@router.post("/changes/preview")
async def preview(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)
):
    require_admin(request)
    content = await read_limited(file, label="accountant's changes")
    return svc.preview(db, content)


@router.post("/changes/apply")
async def apply(
    request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)
):
    """Apply accountant's changes: the new entries post as adjusting
    entries in the accountant's name; a file applied twice posts nothing."""
    require_admin(request)
    content = await read_limited(file, label="accountant's changes")
    out = svc.apply(db, content, posted_by=request.session.get("username") or "")
    logger.info(
        "accountant's changes applied by %s: %s",
        request.session.get("username") or "operator",
        out["counts"],
    )
    return out
