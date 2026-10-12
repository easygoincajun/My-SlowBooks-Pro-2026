# ============================================================================
# Sales tax agencies — the state and the parish (county, city) a sale is
# taxed for, each filed and paid on its own.
#
# A document keeps ONE combined rate, as before. When its tax posts, the
# amount is split across the agencies set up in Settings in proportion to
# their rates, each to its own liability account; the last agency takes the
# rounding cent. With no agencies set up, tax posts to 2200 Sales Tax
# Payable exactly as it always has.
#
# Stored as the company setting "sales_tax_agencies": a JSON list of
# {"name", "rate" (percent, as typed), "account_id"}.
# ============================================================================

import json
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.services.accounting import _q

SETTING = "sales_tax_agencies"


def agencies(db: Session) -> list[dict]:
    """The agencies, rate as a Decimal percent; [] when none are set up."""
    from app.services.settings_service import get_setting_raw

    try:
        rows = json.loads(get_setting_raw(db, SETTING) or "[]")
    except ValueError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            rate = Decimal(str(row["rate"]))
            out.append(
                {
                    "name": str(row["name"]),
                    "rate": rate,
                    "account_id": int(row["account_id"]),
                }
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return out


def save_agencies(db: Session, rows: list[dict]) -> None:
    """Store the agencies and make their total the company's default rate.
    Caller validates and commits."""
    from app.services.settings_service import set_setting

    set_setting(
        db,
        SETTING,
        json.dumps(
            [
                {
                    "name": r["name"],
                    "rate": str(r["rate"]),
                    "account_id": r["account_id"],
                }
                for r in rows
            ]
        ),
    )
    if rows:
        total = sum((Decimal(str(r["rate"])) for r in rows), Decimal("0"))
        set_setting(db, "default_tax_rate", format(total.normalize(), "f"))


def split_tax(db: Session, amount, default_account_id) -> list[tuple]:
    """[(account_id, amount, agency name or None)] adding up to ``amount``."""
    amount = _q(Decimal(str(amount or 0)))
    rows = agencies(db)
    total_rate = sum((r["rate"] for r in rows), Decimal("0"))
    if not rows or total_rate <= 0:
        return [(default_account_id, amount, None)] if amount else []
    out, left = [], amount
    for i, r in enumerate(rows):
        part = left if i == len(rows) - 1 else _q(amount * r["rate"] / total_rate)
        left -= part
        if part:
            out.append((r["account_id"], part, r["name"]))
    return out


def tax_journal_lines(
    db: Session, amount, default_account_id, *, debit=False, description="Sales tax"
) -> list[dict]:
    """Journal lines carrying ``amount`` of sales tax: credits when it is
    collected, debits (``debit=True``) when a credit memo takes it back."""
    zero = Decimal("0")
    return [
        {
            "account_id": account_id,
            "debit": part if debit else zero,
            "credit": zero if debit else part,
            "description": f"{description}: {name}" if name else description,
        }
        for account_id, part, name in split_tax(db, amount, default_account_id)
    ]


def tax_account_ids(db: Session) -> list[int]:
    """Every account sales tax posts to: 2200 and each agency's."""
    from app.services import control_accounts

    main = control_accounts.find(db, "2200")
    ids = [main] if main is not None else []
    for r in agencies(db):
        if r["account_id"] not in ids:
            ids.append(r["account_id"])
    return ids
