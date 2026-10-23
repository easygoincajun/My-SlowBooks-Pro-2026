"""Reclassify (#337): the lines on an account, and the move.

GET  /api/reclassify/lines   an account's lines in a period, each with
                             whether it can move and why not
POST /api/reclassify         move the ticked lines to an account and/or a
                             class; dry_run (the default) answers with the
                             plan and changes nothing
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.common import StrictModel
from app.services import reclassify as svc

router = APIRouter(prefix="/api/reclassify", tags=["reclassify"])


class ReclassifyRequest(StrictModel):
    line_ids: list[int] = Field(..., min_length=1, max_length=5000)
    # the account the lines go to; None leaves the account as it is
    account_id: Optional[int] = None
    # the class they get; None leaves it; clear_class removes the line's own
    class_id: Optional[int] = None
    clear_class: bool = False
    dry_run: bool = True


@router.get("/lines")
def lines(
    account_id: int = Query(...),
    start_date: date = Query(...),
    end_date: date = Query(...),
    db: Session = Depends(get_db),
):
    return svc.lines_on_account(db, account_id, start_date, end_date)


@router.post("")
def reclassify(data: ReclassifyRequest, db: Session = Depends(get_db)):
    return svc.reclassify(
        db,
        data.line_ids,
        account_id=data.account_id,
        class_id=data.class_id,
        clear_class=data.clear_class,
        dry_run=data.dry_run,
    )
