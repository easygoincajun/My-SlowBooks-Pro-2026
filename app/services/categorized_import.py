"""A categorized bank sheet as posted transactions (#328): the Transaction
Pro / SaasAnt layout.

A bookkeeper catching up a client's books cleans the bank export in a
spreadsheet, adds the account each line belongs to, and bulk-posts the
sheet. The sheet is the finished work. Its columns, read by name so the
order does not matter: a **Bank Account** column (the bookkeeper's
template names the account in the header itself, "Bank Account BofA
9959", and leaves the cells empty; a sheet may name it per row instead),
**Transaction Date**, **Memo**, **Payee**, **Account** (the offset: the
expense, income, or other account, by number, name or Parent:Child path)
and **Amount** (money out negative, money in positive, unless the sheet
says the other way round).

Each row posts as the register entry it is: money out or in on the bank
account against its account, a transfer when the account is another bank
or card account; rows that share the bank account, date, payee and memo
post as one split entry. Before anything is written the whole file is
checked (every bank account and account found, every date and amount
read) and refused whole with the reasons. A row already in the books
(same day, same lines) posts nothing twice. Payees not on the Vendors
(money out) or Customers (money in) list are added and named in the
result.

Built around a bookkeeper's own template; the fixture is an invented
sheet in that layout. Nothing of a real file is here.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.accounts import Account
from app.models.contacts import Customer, Vendor

NAME = "Categorized transactions (Transaction Pro / SaasAnt layout)"
COLUMNS = ("Transaction Date", "Memo", "Payee", "Account", "Amount")
BANK_HEADER = "Bank Account"
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d-%b-%Y", "%b %d, %Y")
_HOW = (
    "The sheet's columns are Bank Account (named in the header or on each row), "
    "Transaction Date, Memo, Payee, Account and Amount, in any order."
)


def find_header(rows: list[list[str]]):
    """(header_at, cols, bank_name) for the categorized layout, or
    (None, None, None). The bank column's header may carry the account's
    name after the words Bank Account."""
    for i, row in enumerate(rows):
        cells = [c.strip() for c in row]
        if not all(col in cells for col in COLUMNS):
            continue
        bank_i = next(
            (
                j
                for j, c in enumerate(cells)
                if c.lower().startswith(BANK_HEADER.lower())
            ),
            None,
        )
        if bank_i is None:
            continue
        cols = {name: cells.index(name) for name in COLUMNS}
        cols["Bank Account"] = bank_i
        bank_name = cells[bank_i][len(BANK_HEADER) :].strip(" :-")
        return i, cols, bank_name
    return None, None, None


def is_categorized(rows: list[list[str]]) -> bool:
    return find_header(rows)[0] is not None


def _money(raw: str) -> Decimal:
    raw = (raw or "").replace(",", "").replace("$", "").strip()
    if not raw:
        raise ValueError("empty")
    negative = (raw.startswith("(") and raw.endswith(")")) or raw.startswith("-")
    value = Decimal(raw.strip("()-+ "))
    return -value if negative else value


def _date(raw: str) -> date:
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(raw)


def parse_sheet(rows: list[list[str]], *, outflow_positive: bool = False):
    """(lines, errors): each row as {bank, date, memo, payee, account,
    amount (money out negative), row}."""
    header_at, cols, header_bank = find_header(rows)
    if header_at is None:
        return [], ["This is not the categorized layout. " + _HOW]
    lines, errors = [], []

    def cell(row, name):
        i = cols.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    for n, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        if not any(c.strip() for c in row):
            continue
        bank = cell(row, "Bank Account") or header_bank
        day_raw, amount_raw = cell(row, "Transaction Date"), cell(row, "Amount")
        account = cell(row, "Account")
        if not bank:
            errors.append(
                f"Row {n}: no bank account (name it in the header or on the row)"
            )
        if not account:
            errors.append(
                f"Row {n}: no Account (the expense, income or other account the line posts to)"
            )
        try:
            day = _date(day_raw)
        except ValueError:
            errors.append(f"Row {n}: the date ({day_raw!r}) could not be read")
            continue
        try:
            amount = _money(amount_raw)
        except (ValueError, InvalidOperation):
            errors.append(f"Row {n}: the amount ({amount_raw!r}) could not be read")
            continue
        if amount == 0:
            continue
        if outflow_positive:
            amount = -amount
        lines.append(
            {
                "bank": bank,
                "date": day,
                "memo": cell(row, "Memo"),
                "payee": cell(row, "Payee"),
                "account": account,
                "amount": amount,
                "row": n,
            }
        )
    return lines, errors


def _bank_account(db: Session, name: str):
    """A bank or card account by its name as the sheet gives it: the whole
    name, then the name without the words around it, then by the digits
    at its end ("BofA 9959" finds "Bank of America ...9959")."""
    from app.services.iif_import import _find_account

    banks = [a for a in db.query(Account).all() if a.bank_kind]
    exact = _find_account(db, name)
    if exact is not None and exact.bank_kind:
        return exact
    low = name.lower()
    for a in banks:
        if a.name.lower() == low:
            return a
    for a in banks:
        if low in a.name.lower() or a.name.lower() in low:
            return a
    digits = re.search(r"(\d{3,})\s*$", name)
    if digits:
        tail = digits.group(1)
        hits = [a for a in banks if (a.name or "").rstrip().endswith(tail)]
        if len(hits) == 1:
            return hits[0]
    return None


def _offset_account(db: Session, name: str, find):
    """The Account column: a number first ("6300"), then a name or a
    Parent:Child path, then "6300 Insurance" as a number and a name."""
    name = (name or "").strip()
    if not name:
        return None
    by_number = db.query(Account).filter(Account.account_number == name).first()
    if by_number is not None:
        return by_number
    found = find(db, name)
    if found is not None:
        return found
    head = name.split(" ", 1)[0]
    if head.isdigit():
        return db.query(Account).filter(Account.account_number == head).first()
    return None


def _payee_record(db: Session, name: str, money_in: bool, added: list) -> None:
    """A payee not on the list is added to it: Vendors for money out,
    Customers for money in."""
    name = (name or "").strip()
    if not name:
        return
    model = Customer if money_in else Vendor
    if db.query(model).filter(func.lower(model.name) == name.lower()).first():
        return
    db.add(model(name=name, is_active=True))
    db.flush()
    added.append(f"{name} ({'customer' if money_in else 'vendor'})")


def import_sheet(
    db: Session, rows: list[list[str]], *, outflow_positive: bool = False
) -> dict:
    from app.services.iif_import import _find_account, _same_entries
    from app.services.bank_posting import post_bank_entry

    result = {
        "posted": 0,
        "splits": 0,
        "duplicates_skipped": 0,
        "payees_added": [],
        "errors": [],
        "warnings": [],
        "report": NAME,
    }
    lines, errors = parse_sheet(rows, outflow_positive=outflow_positive)
    if errors:
        # The whole list, however long: it stopped at twenty with "and 14,070
        # more", and the person asked where the rest were (discussion #349).
        # The page counts them and saves the list as a file.
        result["errors"] = errors
        return result
    if not lines:
        result["errors"].append("The sheet has no rows with an amount. " + _HOW)
        return result
    # every account first: the file is refused whole, never posted in part
    banks, accounts, missing_banks, missing = {}, {}, [], []
    for name in OrderedDict.fromkeys(ln["bank"] for ln in lines):
        acct = _bank_account(db, name)
        if acct is None:
            missing_banks.append(name)
        banks[name] = acct
    for name in OrderedDict.fromkeys(ln["account"] for ln in lines):
        acct = _offset_account(db, name, _find_account)
        if acct is None:
            missing.append(name)
        accounts[name] = acct
    if missing_banks:
        result["errors"].append(
            "No bank or card account here is named "
            + ", ".join(f"'{b}'" for b in missing_banks)
            + ". Flag the account as a bank account on the Chart of Accounts, or name "
            "it in the sheet as it is named here. Nothing was posted."
        )
    if missing:
        result["errors"].append(
            f"{len(missing)} account(s) in the sheet are not in the chart: "
            + ", ".join(missing[:12])
            + (" …" if len(missing) > 12 else "")
            + ". Add them, or name them as the chart does (number, name or "
            "Parent:Child). Nothing was posted."
        )
    if result["errors"]:
        return result
    # rows that share the bank, the day, the payee and the memo are one
    # entry split across their accounts
    groups: OrderedDict = OrderedDict()
    for ln in lines:
        groups.setdefault((ln["bank"], ln["date"], ln["payee"], ln["memo"]), []).append(
            ln
        )
    seen: dict = {}
    for (bank_name, day, payee, memo), parts in groups.items():
        bank = banks[bank_name]
        total = sum((p["amount"] for p in parts), Decimal("0"))
        if len(parts) > 1 and (
            total == 0
            or any(p["amount"] > 0 for p in parts)
            != all(p["amount"] > 0 for p in parts)
        ):
            # money both ways under one payee and memo: separate entries
            # after all, each its own
            singles = parts
        else:
            singles = None
        for entry_parts in ([parts] if singles is None else [[p] for p in singles]):
            amount = sum((p["amount"] for p in entry_parts), Decimal("0"))
            if len(entry_parts) > 1:
                lines_posted = [
                    {
                        "account_id": accounts[p["account"]].id,
                        "debit": -p["amount"] if amount < 0 else Decimal("0"),
                        "credit": p["amount"] if amount > 0 else Decimal("0"),
                    }
                    for p in entry_parts
                ]
            else:
                p = entry_parts[0]
                cat = accounts[p["account"]]
                lines_posted = [
                    {
                        "account_id": cat.id,
                        "debit": -amount if amount < 0 else Decimal("0"),
                        "credit": amount if amount > 0 else Decimal("0"),
                    }
                ]
            lines_posted.append(
                {
                    "account_id": bank.id,
                    "debit": amount if amount > 0 else Decimal("0"),
                    "credit": -amount if amount < 0 else Decimal("0"),
                }
            )
            key = (
                day,
                tuple(
                    sorted(
                        (ln["account_id"], ln["debit"], ln["credit"])
                        for ln in lines_posted
                    )
                ),
            )
            nth = seen.get(key, 0) + 1
            seen[key] = nth
            if _same_entries(db, day, lines_posted) >= nth:
                result["duplicates_skipped"] += 1
                continue
            if len(entry_parts) > 1:
                post_bank_entry(
                    db,
                    bank,
                    day,
                    amount,
                    accounts[entry_parts[0]["account"]],
                    payee=payee or None,
                    memo=memo or None,
                    splits=[
                        (accounts[p["account"]], p["amount"], p["memo"] or None)
                        for p in entry_parts
                    ],
                )
                result["splits"] += 1
            else:
                p = entry_parts[0]
                post_bank_entry(
                    db,
                    bank,
                    day,
                    amount,
                    accounts[p["account"]],
                    payee=payee or None,
                    memo=memo or None,
                )
            _payee_record(db, payee, amount > 0, result["payees_added"])
            result["posted"] += 1
    db.commit()
    if result["payees_added"]:
        names = result["payees_added"]
        result["warnings"].append(
            f"{len(names)} payee(s) in the sheet were not on the Vendors or Customers "
            "list and were added from their names: "
            + "; ".join(names[:20])
            + (" …" if len(names) > 20 else "")
        )
    if result["splits"]:
        result["warnings"].append(
            f"{result['splits']} entr{'y' if result['splits'] == 1 else 'ies'} posted as "
            "a split: rows sharing the bank account, date, payee and memo are one entry "
            "across their accounts."
        )
    return result
