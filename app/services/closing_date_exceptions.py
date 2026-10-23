"""Closing Date Exceptions (#342): what moved inside a closed period.

Posting into a closed period takes the closing-date password. Each use
writes an OVERRIDE audit row (closing_date._record_override: the closing
date in force and the transaction's date), and from 2.24.0 every row that
request changes after it carries the audit source "closing_date_override"
(audit._after_flush). This report reads the two together: one event per
override, with the rows it touched (which record, what action, each field
before and after), for the period in which the changes were made. The
report QuickBooks accountants check at every review, by the same name.

An override recorded before 2.24.0 shows its dates and who made it; the
rows it changed carried the ordinary source then, so they are not listed.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json

from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.services.closing_date import get_closing_date

OVERRIDE_SOURCE = "closing_date_override"

# What a table's rows are called on the report
_RECORD_WORDS = {
    "transactions": "journal entry",
    "transaction_lines": "journal line",
    "invoices": "invoice",
    "invoice_lines": "invoice line",
    "bills": "bill",
    "bill_lines": "bill line",
    "bill_payments": "bill payment",
    "payments": "payment",
    "payment_allocations": "payment allocation",
    "credit_memos": "credit memo",
    "vendor_credits": "vendor credit",
    "deposits": "deposit",
    "sales_receipts": "sales receipt",
    "estimates": "estimate",
    "cc_charges": "card charge",
    "expenses": "expense",
    "accounts": "account",
    "settings": "setting",
}
# The fields that name a record, in the order tried
_NAME_FIELDS = (
    "invoice_number",
    "bill_number",
    "receipt_number",
    "estimate_number",
    "check_number",
    "number",
    "reference",
    "description",
    "memo",
    "name",
    "key",
)
# Bookkeeping fields nobody needs to see change
_QUIET_FIELDS = {"updated_at", "created_at", "balance"}


def record_word(table_name: str) -> str:
    if table_name in _RECORD_WORDS:
        return _RECORD_WORDS[table_name]
    word = table_name.replace("_", " ")
    return word[:-1] if word.endswith("s") else word


def _label(values: dict | None) -> str:
    values = values or {}
    for key in _NAME_FIELDS:
        v = values.get(key)
        if v not in (None, "", "***"):
            return str(v)
    return ""


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


# A field that names a record by its id: shown by the record's name
# ("account_id 54" read as a database id: 2.24.0 gate, NEW-153 / W-19)
_NAMED_IDS = {"account_id": "account", "class_id": "class"}


def _named(field: str, value, names: dict | None):
    """(field as shown, value as shown): an account's or a class's id by
    its name when the names are known."""
    if names and field in _NAMED_IDS and value not in (None, ""):
        try:
            key = int(value)
        except (TypeError, ValueError):
            return field, _text(value)
        if key in names.get(field, {}):
            return _NAMED_IDS[field], names[field][key]
    return field, _text(value)


def _fields(row: AuditLog, names: dict | None = None) -> list[dict]:
    """Each field a change moved, before and after. An INSERT lists the
    new row's named fields; a DELETE the old row's. Secrets are redacted
    (NEW-148); an account's or a class's id is shown by its name."""
    from app.services.audit_redaction import redact_values

    if row.action == "UPDATE":
        fields = [f for f in (row.changed_fields or []) if f not in _QUIET_FIELDS]
        old = redact_values(row.table_name, row.old_values) or {}
        new = redact_values(row.table_name, row.new_values) or {}
        out = []
        for f in fields:
            shown, before = _named(f, old.get(f), names)
            _, after = _named(f, new.get(f), names)
            out.append({"field": shown, "before": before, "after": after})
        return out
    values = row.new_values if row.action == "INSERT" else (row.old_values or {})
    values = redact_values(row.table_name, values or {}) or {}
    shown = [
        f
        for f in (
            "date",
            *_NAME_FIELDS,
            "total",
            "amount",
            "total_amount",
            "debit",
            "credit",
            "account_id",
            "status",
        )
        if f in values and values.get(f) not in (None, "", "***")
    ]
    if row.action == "INSERT":
        return [
            {
                "field": _named(f, values[f], names)[0],
                "before": "",
                "after": _named(f, values[f], names)[1],
            }
            for f in shown
        ]
    return [
        {
            "field": _named(f, values[f], names)[0],
            "before": _named(f, values[f], names)[1],
            "after": "",
        }
        for f in shown
    ]


# for the package's audit-log CSV (#343)
fields_of = _fields
label_of = _label


def record_names(db: Session) -> dict:
    """The names an id-field is shown by: accounts and classes."""
    from app.models.accounts import Account
    from app.models.classes import TxnClass

    accounts = {}
    for a in db.query(Account).all():
        number = (a.account_number or "").strip()
        accounts[a.id] = f"{number} {a.name}".strip()
    return {
        "account_id": accounts,
        "class_id": {c.id: c.name for c in db.query(TxnClass).all()},
    }


# ---- the period and the clock ---------------------------------------------
# The log's timestamps are UTC. The person's period is in their own day, and
# their clock reads local time: an override made at 7:12 PM Central on Oct 19
# is stored as Oct 20 00:12 and was missing from "This Year to Date" on Oct
# 19, and shown as "00:12" with no zone (2.24.0 gate, NEW-152 / W-19). The
# page sends its clock's offset from UTC (JavaScript's getTimezoneOffset:
# minutes to add to local time to reach UTC, 300 for Central Daylight Time);
# the period's bounds and every time shown follow it.


def utc_bounds(start_date: date, end_date: date, tz_offset: int = 0):
    """The UTC instants a local period runs between: [start, end)."""
    shift = timedelta(minutes=int(tz_offset or 0))
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc) + shift
    end = (
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        + shift
    )
    return start, end


def local_words(stamp, tz_offset: int = 0) -> str:
    """A UTC timestamp as the person's clock reads it: 'YYYY-MM-DD HH:MM'."""
    if not stamp:
        return ""
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return (stamp - timedelta(minutes=int(tz_offset or 0))).strftime("%Y-%m-%d %H:%M")


def zone_words(tz_offset: int = 0) -> str:
    """'UTC−05:00' for an offset of 300 (minutes west of UTC)."""
    minutes = -int(tz_offset or 0)
    sign = "+" if minutes >= 0 else "−"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def _change(row: AuditLog, names: dict | None = None, tz_offset: int = 0) -> dict:
    values = row.new_values if row.action != "DELETE" else row.old_values
    return {
        "audit_id": row.id,
        "when": row.timestamp.isoformat() if row.timestamp else "",
        "when_local": local_words(row.timestamp, tz_offset),
        "who": row.username or "",
        "table": row.table_name,
        "record": record_word(row.table_name),
        "record_id": row.record_id,
        "label": _label(values),
        "action": row.action.lower(),
        "fields": _fields(row, names),
    }


def closing_date_exceptions(
    db: Session, start_date: date, end_date: date, tz_offset: int = 0
) -> dict:
    """The overrides made in the period (by the day they were made, on the
    person's clock), each with the rows it changed."""
    start_utc, end_utc = utc_bounds(start_date, end_date, tz_offset)
    overrides = (
        db.query(AuditLog)
        .filter(
            AuditLog.table_name == "closing_date",
            AuditLog.action == "OVERRIDE",
            AuditLog.timestamp >= start_utc,
            AuditLog.timestamp < end_utc,
        )
        .order_by(AuditLog.id)
        .all()
    )
    names = record_names(db) if overrides else None
    events = []
    for ov in overrides:
        values = ov.new_values or {}
        events.append(
            {
                "audit_id": ov.id,
                "when": ov.timestamp.isoformat() if ov.timestamp else "",
                "when_local": local_words(ov.timestamp, tz_offset),
                "who": ov.username or "",
                "closing_date": values.get("closing_date") or "",
                "transaction_date": values.get("transaction_date") or "",
                "changes": [],
            }
        )
    if overrides:
        changed = (
            db.query(AuditLog)
            .filter(
                AuditLog.source == OVERRIDE_SOURCE,
                AuditLog.table_name != "closing_date",
                AuditLog.id > overrides[0].id,
            )
            .order_by(AuditLog.id)
            .all()
        )
        # A change belongs to the latest override before it by the same
        # sign-in: an override's request writes its own row first, and
        # another person's request writes under their own name.
        by_user: dict[str, list[dict]] = {}
        for ev in events:
            by_user.setdefault(ev["who"], []).append(ev)
        for row in changed:
            mine = by_user.get(row.username or "")
            if not mine:
                continue
            owner = None
            for ev in mine:
                if ev["audit_id"] < row.id:
                    owner = ev
                else:
                    break
            if owner is not None:
                change = _change(row, names, tz_offset)
                # an account's balance moving with the posting is not a
                # change anyone made: nothing to show, so nothing listed
                if change["action"] == "update" and not change["fields"]:
                    continue
                owner["changes"].append(change)
    closing = get_closing_date(db)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "closing_date": closing.isoformat() if closing else "",
        "tz_offset": int(tz_offset or 0),
        "timezone": zone_words(tz_offset),
        "events": events,
        "count": len(events),
        "changes": sum(len(ev["changes"]) for ev in events),
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def exceptions_csv(data: dict, company: str) -> str:
    """One row per field a change moved (a change with no field listed
    gets one row of its own), the event's words on every row."""
    import io

    from app.services.csv_export import _SafeWriter

    out = io.StringIO()
    w = _SafeWriter(out)
    w.writerow(["Company", company])
    w.writerow(["Report", "Closing Date Exceptions"])
    w.writerow(["Period", f"{data['start_date']} to {data['end_date']}"])
    w.writerow(["Times in", data.get("timezone", "UTC")])
    w.writerow([])
    w.writerow(
        [
            "When",
            "Who",
            "Closing date",
            "Transaction date",
            "Record",
            "Action",
            "Field",
            "Before",
            "After",
        ]
    )
    for ev in data["events"]:
        head = [
            ev.get("when_local") or ev["when"],
            ev["who"],
            ev["closing_date"],
            ev["transaction_date"],
        ]
        if not ev["changes"]:
            w.writerow(head + ["", "override used", "", "", ""])
        for ch in ev["changes"]:
            record = f"{ch['record']} {ch['record_id']}"
            if ch["label"]:
                record += f" ({ch['label']})"
            if not ch["fields"]:
                w.writerow(head + [record, ch["action"], "", "", ""])
            for f in ch["fields"]:
                w.writerow(
                    head + [record, ch["action"], f["field"], f["before"], f["after"]]
                )
    w.writerow([])
    w.writerow(["Overrides", data["count"], "Changes", data["changes"]])
    return out.getvalue()


def exceptions_section(data: dict) -> dict:
    """The report as a PDF section."""
    rows = []
    for ev in data["events"]:
        head = (
            f"{ev.get('when_local') or ev['when'][:16].replace('T', ' ')}  {ev['who']}  "
            f"closing date {ev['closing_date']}, a {ev['transaction_date']} change"
        )
        rows.append({"cells": [head, "", "", ""], "style": "subtotal"})
        if not ev["changes"]:
            rows.append(
                {
                    "cells": [
                        "    (the override was used; the rows it changed were not marked before 2.24.0)",
                        "",
                        "",
                        "",
                    ]
                }
            )
        for ch in ev["changes"]:
            record = f"    {ch['action']} {ch['record']} {ch['record_id']}"
            if ch["label"]:
                record += f" ({ch['label']})"
            if not ch["fields"]:
                rows.append({"cells": [record, "", "", ""]})
            for i, f in enumerate(ch["fields"]):
                rows.append(
                    {
                        "cells": [
                            record if i == 0 else "",
                            f["field"],
                            f["before"],
                            f["after"],
                        ]
                    }
                )
    rows.append(
        {
            "cells": [
                f"{_plural(data['count'], 'override')}, {_plural(data['changes'], 'change')}",
                "",
                "",
                "",
            ],
            "style": "grand-total",
        }
    )
    period = f"{data['start_date']} — {data['end_date']}"
    if data.get("closing_date"):
        period += f" · closing date now {data['closing_date']}"
    period += f" · times in {data.get('timezone', 'UTC')}"
    return {
        "title": "Closing Date Exceptions",
        "period": period,
        "columns": ["Change", "Field", "Before", "After"],
        "rows": rows,
    }
