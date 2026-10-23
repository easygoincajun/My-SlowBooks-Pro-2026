# ============================================================================
# Document audit endpoints — admin-only lookup of audit rows for verification.
#
# These power the "is this PDF authentic?" workflow:
#   1. Operator opens a tax-form PDF, reads "Audit ID #42" from the footer.
#   2. Operator hits GET /api/document-audits/42 to pull the canonical row.
#   3. Compares the hash printed in the PDF to the hash returned by the API.
#      Match -> the PDF data matches what was generated. Mismatch -> tamper
#      or version drift.
# ============================================================================

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.document_audit import DocumentAudit
from app.services.document_check import describe, recheck

router = APIRouter(prefix="/api/document-audits", tags=["document-audit"])


class DocumentAuditResponse(BaseModel):
    id: int
    doc_type: str
    doc_key: str
    content_hash: str
    created_at: Optional[datetime]
    # who generated it (NULL on rows from before 2.22.2), and what it is
    # in words: form "W-2", subject "Jonah Pike, 2026" (#263)
    username: Optional[str] = None
    form: str = ""
    subject: str = ""
    model_config = {"from_attributes": True}


def _row(db: Session, audit: DocumentAudit) -> DocumentAuditResponse:
    form, subject = describe(db, audit)
    r = DocumentAuditResponse.model_validate(audit)
    r.form, r.subject = form, subject
    # stored in UTC; SQLite hands it back naive, and a page would read a
    # bare timestamp as local time (gate round 1, NEW-58)
    if r.created_at is not None and r.created_at.tzinfo is None:
        r.created_at = r.created_at.replace(tzinfo=timezone.utc)
    return r


@router.get("", response_model=list[DocumentAuditResponse])
def list_audits(
    doc_type: Optional[str] = Query(default=None),
    doc_key: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List recent document audit rows, newest first. Optional filters
    narrow by doc family (`w2`, `941`, ...) or by the type-specific key."""
    q = db.query(DocumentAudit)
    if doc_type:
        q = q.filter(DocumentAudit.doc_type == doc_type)
    if doc_key:
        q = q.filter(DocumentAudit.doc_key == doc_key)
    return [_row(db, a) for a in q.order_by(DocumentAudit.id.desc()).limit(limit)]


@router.get("/{audit_id}", response_model=DocumentAuditResponse)
def get_audit(audit_id: int, db: Session = Depends(get_db)):
    """Look up one audit row by its ID — the ID printed in the PDF footer."""
    row = db.query(DocumentAudit).filter(DocumentAudit.id == audit_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Audit row not found")
    return _row(db, row)


@router.get("/verify/{content_hash}", response_model=list[DocumentAuditResponse])
def verify_hash(content_hash: str, db: Session = Depends(get_db)):
    """Find every audit row matching this SHA-256 hash: the full 64, or the
    16 a form's footer prints (gate round 1, W-3 — a printed form could not
    be looked up by its footer), as a prefix. Used when an auditor has the
    PDF but not the ID, or recomputed the hash and wants to confirm it is
    known to the system."""
    h = content_hash.strip().lower()
    if not (16 <= len(h) <= 64) or not all(c in "0123456789abcdef" for c in h):
        raise HTTPException(
            status_code=400,
            detail="content_hash must be 16 to 64 hex characters (the footer's 16, or the full hash)",
        )
    q = db.query(DocumentAudit)
    q = (
        q.filter(DocumentAudit.content_hash == h)
        if len(h) == 64
        else q.filter(DocumentAudit.content_hash.like(h + "%"))
    )
    return [_row(db, a) for a in q.order_by(DocumentAudit.id.desc())]


@router.get("/{audit_id}/check")
def check_audit(audit_id: int, db: Session = Depends(get_db)):
    """Re-figure the form from today's books and say, in a sentence,
    whether it still matches what was generated (#263). The verify route
    answers whether a hash is known; this answers whether the books behind
    a known form are as they were."""
    row = db.query(DocumentAudit).filter(DocumentAudit.id == audit_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Audit row not found")
    return recheck(db, row)
