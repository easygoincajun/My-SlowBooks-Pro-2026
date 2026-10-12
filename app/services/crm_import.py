# ============================================================================
# CRM export import — the CSV export of the in-house CRM this company kept
# its sales and expenses in before SlowBooks (contacts.csv, invoices.csv,
# payments.csv, products_and_services.csv, expenses.csv,
# account_transactions.csv). Built from a real export, per CONTRIBUTING.
#
# Two steps, both run by scripts/import_crm_export.py:
#
#   plan:   read the export, decide what each record becomes, and write the
#           match files (match/customers.csv, items.csv, accounts.csv) plus
#           report.txt. Nothing is written to the books. A person edits the
#           match files (rename, merge, pick accounts) and plans again; the
#           edits are read back in and win over the defaults.
#   apply:  the same plan, posted in the caller's session. The caller
#           commits once, so an import lands whole or not at all.
#
# What the export says and how it's read:
# - A contact is referenced by EMAIL; one without an email as
#   "Name (Company)". Only contacts that were invoiced become customers.
# - invoices.csv is one row per line, the invoice's own fields repeated.
#   There is no invoice date: the day it was sent is used.
# - Money is "$1,234.56 " and, where a spreadsheet touched it, "'-$60.00".
# - A negative payment is the reversal of a duplicate one; it cancels the
#   positive payment of the same amount and method on the same invoice.
# - An expense has no paid-from account; it comes from accounts.csv, by
#   category.
#
# Postings use what the screens use: _post_invoice_journal for an invoice,
# the payment's DR bank / CR A/R, an expense's DR category / CR paid-from,
# post_transfer between two bank or card accounts.
# ============================================================================

import csv
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.contacts import Customer, Vendor
from app.models.invoices import Invoice, InvoiceLine, InvoiceStatus
from app.models.items import Item, ItemType
from app.models.payments import Payment, PaymentAllocation
from app.models.transactions import Transaction
from app.services.accounting import (
    _q,
    create_journal_entry,
    get_ar_account_id,
)
from app.services.iif_import import _named

MATCH_DIR = "match"
SOURCE = "CRM import"

CUSTOMERS_HEADER = [
    "source",
    "slowbooks_name",
    "email",
    "phone",
    "address",
    "invoices",
]
ITEMS_HEADER = [
    "source_title",
    "slowbooks_item",
    "item_type",
    "income_account",
    "price",
    "lines",
]
ACCOUNTS_HEADER = [
    "source_name",
    "used_as",
    "slowbooks_account",
    "new_account_type",
    "paid_from",
    "rows",
    "total",
    "note",
]
# What a new account can be made as (accounts.csv new_account_type).
NEW_TYPES = {
    "income": (AccountType.INCOME, None),
    "expense": (AccountType.EXPENSE, None),
    "cogs": (AccountType.COGS, None),
    "equity": (AccountType.EQUITY, None),
    "asset": (AccountType.ASSET, None),
    "liability": (AccountType.LIABILITY, None),
    "bank": (AccountType.ASSET, "bank"),
    "credit_card": (AccountType.LIABILITY, "credit_card"),
}
DEPOSIT_ROW = "Customer payments"
# The payment form's own choices (payments.js).
METHODS = {
    "check": "Check",
    "bank_transfer": "ACH/EFT",
    "cash": "Cash",
    "other": "Other",
}
_LABEL_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<company>[^()]*)\)\s*$")
_CITY_RE = re.compile(
    r"^(?P<city>.*?),\s*(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)


def money(raw) -> Decimal:
    """ "$1,234.56 ", "'-$60.00" (a spreadsheet's text marker), "(5.00)"."""
    s = (raw or "").strip().replace("'", "").replace("$", "").replace(",", "")
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-()").strip()
    value = Decimal(s or "0")
    return -value if neg else value


def day(raw) -> date | None:
    raw = (raw or "").strip()
    return date.fromisoformat(raw[:10]) if raw else None


def _rows(folder: Path, name: str) -> list[dict]:
    path = folder / name
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "") for k, v in row.items()} for row in csv.DictReader(f)]


def _label(raw: str) -> tuple[str, str]:
    """ "Name (Company)" -> (name, company); anything else -> (raw, "")."""
    m = _LABEL_RE.match(raw.strip())
    if m:
        return m["name"].strip(), m["company"].strip()
    return raw.strip(), ""


def _payee_name(raw: str) -> str:
    name, company = _label(raw)
    return company or name


def _address(raw: str) -> dict:
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    out = {}
    if lines and (m := _CITY_RE.match(lines[-1])):
        out.update(
            bill_city=m["city"], bill_state=m["state"].upper(), bill_zip=m["zip"]
        )
        lines = lines[:-1]
    if lines:
        out["bill_address1"] = lines[0][:200]
    if len(lines) > 1:
        out["bill_address2"] = ", ".join(lines[1:])[:200]
    return out


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    customers: dict = field(default_factory=OrderedDict)  # source -> row
    items: dict = field(default_factory=OrderedDict)  # source title -> row
    accounts: dict = field(default_factory=OrderedDict)  # (used_as, source) -> row
    invoices: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    expenses: list = field(default_factory=list)
    transfers: list = field(default_factory=list)
    problems: list = field(default_factory=list)  # block the import
    notes: list = field(default_factory=list)  # said in the report
    totals: dict = field(default_factory=dict)


def _read_edits(path: Path, key) -> dict:
    """A match file the person edited, keyed like the plan's rows."""
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {key(row): row for row in csv.DictReader(f)}


def _existing_account(db: Session, name: str):
    return _named(db, Account, name) if name else None


def _account_guess(db: Session, used_as: str, source: str) -> tuple[str, str, str]:
    """(slowbooks_account, new_account_type, note) for a source account."""
    low = source.lower()
    found = _existing_account(db, source)
    if found:
        return found.name, "", "same name"
    if used_as in ("transfer", "expense"):
        # an "expense" category can be the owner taking money out, too
        if "draw" in low or (used_as == "transfer" and "personal" in low):
            return "Owner's Draw", "", "money taken out by the owner"
        if "contribution" in low or "investment" in low or "capital" in low:
            return "Owner's Equity", "", "money put in by the owner"
    if used_as == "transfer":
        if "checking" in low:
            return "Checking", "", "the bank account"
        return source, "bank", ""
    if used_as == "expense":
        if "card" in low or "amex" in low:
            return "Credit Card", "", "a card payment, not an expense: check it"
        if "cost of goods" in low:
            return source, "cogs", ""
        return source, "expense", ""
    if used_as == "income":
        return source, "income", ""
    return source, "", ""


def build_plan(db: Session, folder: Path) -> Plan:
    folder = Path(folder)
    match = folder / MATCH_DIR
    plan = Plan()
    contacts = _rows(folder, "contacts.csv")
    lines = _rows(folder, "invoices.csv")
    payments = _rows(folder, "payments.csv")
    products = _rows(folder, "products_and_services.csv")
    expenses = _rows(folder, "expenses.csv")
    moves = _rows(folder, "account_transactions.csv")
    if _rows(folder, "credit_memos.csv"):
        plan.problems.append(
            "credit_memos.csv has rows; this import doesn't bring credit memos."
        )

    # -- accounts: every source account the books need, with the edits ----
    edits = _read_edits(
        match / "accounts.csv", lambda r: (r["used_as"], r["source_name"])
    )
    used = Counter()
    totals = Counter()

    def account_row(used_as, source, amount=Decimal("0")):
        key = (used_as, source)
        used[key] += 1
        totals[key] += amount
        if key not in plan.accounts:
            name, new_type, note = _account_guess(db, used_as, source)
            row = {
                "source_name": source,
                "used_as": used_as,
                "slowbooks_account": name,
                "new_account_type": new_type,
                "paid_from": "",
                "note": note,
            }
            if used_as == "expense":
                row["paid_from"] = "Checking"
            if key in edits:
                for col in ("slowbooks_account", "new_account_type", "paid_from"):
                    row[col] = (edits[key].get(col) or "").strip()
            plan.accounts[key] = row
        return plan.accounts[key]

    account_row("deposit", DEPOSIT_ROW)
    if (("deposit", DEPOSIT_ROW)) not in edits:
        plan.accounts[("deposit", DEPOSIT_ROW)]["slowbooks_account"] = "Checking"
        plan.accounts[("deposit", DEPOSIT_ROW)]["note"] = "where customer payments go"

    # -- items: the catalog, and every line title -------------------------
    catalog = {p["name"].strip().lower(): p for p in products if p["name"].strip()}
    item_edits = _read_edits(match / "items.csv", lambda r: r["source_title"])
    titles = Counter(r["lineTitle"].strip() for r in lines if r["lineTitle"].strip())
    last_price = {}
    for r in lines:
        last_price[r["lineTitle"].strip()] = money(r["unitPrice"])
    for title in list(
        dict.fromkeys([p["name"].strip() for p in products] + list(titles))
    ):
        if not title or title.lower() in {t.lower() for t in plan.items}:
            continue
        product = catalog.get(title.lower())
        category = (product or {}).get("incomeCategoryName", "").strip()
        row = {
            "source_title": title,
            "slowbooks_item": product["name"].strip() if product else title,
            "item_type": "service",
            "income_account": "",
            "price": str(
                money(product["unitPrice"]) if product else last_price.get(title, 0)
            ),
            "lines": sum(n for t, n in titles.items() if t.lower() == title.lower()),
        }
        if category:
            row["income_account"] = account_row("income", category)["slowbooks_account"]
        if title in item_edits:
            for col in ("slowbooks_item", "item_type", "income_account", "price"):
                row[col] = (item_edits[title].get(col) or "").strip()
        plan.items[title] = row

    # -- customers: who was invoiced --------------------------------------
    by_email = {}
    no_email = {}
    for c in contacts:
        person = f"{c['firstName'].strip()} {c['lastName'].strip()}".strip()
        email = c["email"].strip().lower()
        if email:
            # two rows for one email: the one that names the company wins
            if email not in by_email or (
                c["company"].strip() and not by_email[email]["company"].strip()
            ):
                by_email[email] = c
        else:
            no_email[(person.lower(), c["company"].strip().lower())] = c
    cust_edits = _read_edits(match / "customers.csv", lambda r: r["source"])
    invoices = OrderedDict()
    for r in lines:
        invoices.setdefault(r["invoiceNumber"].strip(), []).append(r)
    counts = Counter(rows[0]["contactEmail"].strip() for rows in invoices.values())
    for source, n in counts.items():
        contact = by_email.get(source.lower())
        if contact is None:
            name, company = _label(source)
            contact = no_email.get((name.lower(), company.lower()))
        if contact is not None:
            person = (
                f"{contact['firstName'].strip()} {contact['lastName'].strip()}".strip()
            )
            default = contact["company"].strip() or person or source
        else:
            name, company = _label(source)
            default = company or name
            plan.notes.append(
                f"Customer {source!r} is not in contacts.csv; named {default!r}."
            )
        row = {
            "source": source,
            "slowbooks_name": default,
            "email": (contact or {}).get("email", "").strip(),
            "phone": (
                (contact or {}).get("businessPhone")
                or (contact or {}).get("phone")
                or ""
            ).strip(),
            "address": (contact or {}).get("address", "").strip(),
            "invoices": n,
        }
        if source in cust_edits:
            row["slowbooks_name"] = (
                cust_edits[source].get("slowbooks_name") or ""
            ).strip()
        if not row["slowbooks_name"]:
            plan.problems.append(f"customers.csv: {source!r} has no slowbooks_name.")
        plan.customers[source] = row
    merged = Counter(r["slowbooks_name"].lower() for r in plan.customers.values())
    for name, n in merged.items():
        if n > 1:
            plan.notes.append(f"{n} source customers become one: {name!r}.")

    # -- invoices ----------------------------------------------------------
    for number, rows in invoices.items():
        head = rows[0]
        sent, due = day(head["sentAt"]), day(head["dueDate"])
        inv_date = sent or due
        due = max(due or inv_date, inv_date)
        doc_lines = []
        for order, r in enumerate(rows):
            qty = Decimal(r["quantity"].strip() or "1")
            rate = money(r["unitPrice"])
            amount = money(r["lineAmount"])
            if _q(qty * rate) != amount:
                plan.problems.append(
                    f"invoices.csv #{number}: line {qty} x {rate} is not {amount}."
                )
            title = r["lineTitle"].strip()
            doc_lines.append(
                {
                    "title": title,
                    "description": r["lineDescription"].strip() or title,
                    "quantity": qty,
                    "rate": rate,
                    "is_taxable": r["taxable"].strip().upper() == "TRUE",
                    "line_order": order,
                }
            )
        subtotal = sum(
            (_q(ln["quantity"] * ln["rate"]) for ln in doc_lines), Decimal("0")
        )
        tax, total = money(head["taxAmount"]), money(head["total"])
        if money(head["discountAmount"]) or money(head["creditedAmount"]):
            plan.problems.append(
                f"invoices.csv #{number}: has a discount or credit; not handled."
            )
        if subtotal + tax != total:
            plan.problems.append(
                f"invoices.csv #{number}: lines {subtotal} + tax {tax} is not the total {total}."
            )
        plan.invoices.append(
            {
                "number": number,
                "customer": head["contactEmail"].strip(),
                "date": inv_date,
                "due_date": due,
                "lines": doc_lines,
                "subtotal": subtotal,
                "tax": tax,
                "total": total,
                "paid": money(head["paidAmount"]),
            }
        )

    # -- payments: a negative one cancels its duplicate -------------------
    numbers = {inv["number"] for inv in plan.invoices}
    kept = []
    for i, p in enumerate(payments):
        p = {**p, "_amount": money(p["amount"]), "_i": i}
        if p["invoiceNumber"].strip() not in numbers:
            plan.problems.append(
                f"Payment {p['amount']} on invoice {p['invoiceNumber']}: no such invoice."
            )
            continue
        kept.append(p)
    for neg in [p for p in kept if p["_amount"] < 0]:
        twins = [
            p
            for p in kept
            if p["invoiceNumber"] == neg["invoiceNumber"]
            and p["_amount"] == -neg["_amount"]
            and p["method"] == neg["method"]
        ]
        if not twins:
            plan.problems.append(
                f"invoices.csv #{neg['invoiceNumber']}: negative payment {neg['amount']} has no matching payment to cancel."
            )
            continue
        twin = min(twins, key=lambda p: p["paidAt"])
        kept = [p for p in kept if p is not twin and p is not neg]
        plan.notes.append(
            f"invoices.csv #{neg['invoiceNumber']}: the {twin['amount'].strip()} payment of "
            f"{twin['paidAt'][:10]} and its reversal of {neg['paidAt'][:10]} cancel; neither is imported."
        )
    paid = Counter()
    for p in kept:
        paid[p["invoiceNumber"].strip()] += p["_amount"]
        note = p["note"].strip()
        plan.payments.append(
            {
                "invoice": p["invoiceNumber"].strip(),
                "date": day(p["paidAt"]),
                "amount": p["_amount"],
                "method": METHODS.get(
                    p["method"].strip(), p["method"].strip().title() or None
                ),
                "check_number": (
                    note if p["method"] == "check" and note.isdigit() else None
                ),
                "notes": note or None,
            }
        )
    for inv in plan.invoices:
        if paid[inv["number"]] != inv["paid"]:
            plan.problems.append(
                f"invoices.csv #{inv['number']}: payments add up to {paid[inv['number']]}, the CRM says {inv['paid']} paid."
            )
        if paid[inv["number"]] > inv["total"]:
            plan.problems.append(
                f"invoices.csv #{inv['number']}: paid more than its total."
            )

    # -- expenses and transfers -------------------------------------------
    for i, e in enumerate(expenses, start=1):
        category = e["categoryName"].strip()
        amount = money(e["lineAmount"])
        account_row("expense", category, amount)
        plan.expenses.append(
            {
                "ref": f"CRM expense {i}",
                "date": day(e["paidDate"]) or day(e["billDate"]),
                "payee": _payee_name(e["payee"]),
                "category": category,
                "amount": amount,
                "memo": e["lineDescription"].strip() or None,
            }
        )
    for i, t in enumerate(moves, start=1):
        if t["reversalOfDate"].strip():
            # the money moving back: posted as it reads, it undoes the original
            plan.notes.append(
                f"Transfer row {i} ({t['fromAccount'].strip()} → {t['toAccount'].strip()}, "
                f"{t['amount'].strip()}) reverses one of {t['reversalOfDate'].strip()}; "
                "posted as a movement back."
            )
        amount = money(t["amount"])
        account_row("transfer", t["fromAccount"].strip(), -amount)
        account_row("transfer", t["toAccount"].strip(), amount)
        plan.transfers.append(
            {
                "ref": f"CRM transfer {i}",
                "date": day(t["occurredOn"]),
                "from": t["fromAccount"].strip(),
                "to": t["toAccount"].strip(),
                "amount": amount,
                "memo": t["memo"].strip() or None,
            }
        )
    for key, row in plan.accounts.items():
        row["rows"] = used[key]
        row["total"] = str(totals[key])
        if not row["slowbooks_account"]:
            plan.problems.append(
                f"accounts.csv: {key[1]!r} ({key[0]}) has no slowbooks_account."
            )
        elif (
            not _existing_account(db, row["slowbooks_account"])
            and row["new_account_type"] not in NEW_TYPES
        ):
            plan.problems.append(
                f"accounts.csv: {row['slowbooks_account']!r} isn't in the chart; give it a "
                f"new_account_type ({', '.join(NEW_TYPES)})."
            )
        if key[0] == "expense" and not row["paid_from"]:
            plan.problems.append(
                f"accounts.csv: expense category {key[1]!r} has no paid_from."
            )
    known = {r["slowbooks_account"].lower() for r in plan.accounts.values()}
    for row in plan.items.values():
        acct = row["income_account"]
        if acct and acct.lower() not in known and not _existing_account(db, acct):
            plan.problems.append(
                f"items.csv: income account {acct!r} for {row['source_title']!r} is neither in "
                "the chart nor in accounts.csv."
            )
        if row["item_type"] not in {t.value for t in ItemType}:
            plan.problems.append(
                f"items.csv: {row['source_title']!r} has item_type {row['item_type']!r}."
            )

    open_ = [inv for inv in plan.invoices if inv["total"] - paid[inv["number"]] > 0]
    plan.totals = {
        "invoices": len(plan.invoices),
        "invoiced": sum((inv["total"] for inv in plan.invoices), Decimal("0")),
        "payments": len(plan.payments),
        "paid": sum((p["amount"] for p in plan.payments), Decimal("0")),
        "open_invoices": len(open_),
        "open_ar": sum(
            (inv["total"] - paid[inv["number"]] for inv in open_), Decimal("0")
        ),
        "expenses": len(plan.expenses),
        "expense_total": sum((e["amount"] for e in plan.expenses), Decimal("0")),
        "transfers": len(plan.transfers),
        "customers": len(
            {r["slowbooks_name"].lower() for r in plan.customers.values()}
        ),
        "items": len({r["slowbooks_item"].lower() for r in plan.items.values()}),
    }
    return plan


def write_match_files(plan: Plan, folder: Path) -> Path:
    out = Path(folder) / MATCH_DIR
    out.mkdir(exist_ok=True)
    for name, header, rows in (
        ("customers.csv", CUSTOMERS_HEADER, plan.customers.values()),
        ("items.csv", ITEMS_HEADER, plan.items.values()),
        ("accounts.csv", ACCOUNTS_HEADER, plan.accounts.values()),
    ):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    (out / "report.txt").write_text(report(plan), encoding="utf-8")
    return out


def report(plan: Plan) -> str:
    t = plan.totals
    out = [
        "CRM import plan",
        "===============",
        f"Customers: {t['customers']}   Items: {t['items']}",
        f"Invoices: {t['invoices']}, total ${t['invoiced']:,.2f}",
        f"Payments: {t['payments']}, total ${t['paid']:,.2f}",
        f"Open A/R: {t['open_invoices']} invoices, ${t['open_ar']:,.2f}",
        f"Expenses: {t['expenses']}, total ${t['expense_total']:,.2f}",
        f"Transfers: {t['transfers']}",
        "",
    ]
    if plan.problems:
        out += [f"PROBLEMS ({len(plan.problems)}) - fix these before importing:"]
        out += [f"  - {p}" for p in plan.problems] + [""]
    else:
        out += ["No problems: ready to import.", ""]
    if plan.notes:
        out += ["Notes:"] + [f"  - {n}" for n in plan.notes]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Applying the plan
# ---------------------------------------------------------------------------


def _account(db: Session, name: str, new_type: str = "") -> Account:
    found = _existing_account(db, name)
    if found:
        return found
    account_type, bank_kind = NEW_TYPES[new_type]
    acct = Account(
        name=name, account_type=account_type, bank_kind=bank_kind, is_active=True
    )
    db.add(acct)
    db.flush()
    return acct


def _already_posted(db: Session, ref: str) -> bool:
    return (
        db.query(Transaction.id).filter(Transaction.reference == ref).first()
        is not None
    )


def apply_plan(db: Session, plan: Plan) -> dict:
    """Post the plan in ``db`` without committing. Refuses a plan with problems.
    What already exists (an invoice number, a name, an expense's reference)
    is left as it is, so a second run adds nothing."""
    from app.routes.invoices.helpers import _post_invoice_journal
    from app.services.bank_posting import post_transfer

    if plan.problems:
        raise ValueError(
            f"The plan has {len(plan.problems)} problem(s); see report.txt."
        )
    made = Counter()
    accounts = {}
    for row in plan.accounts.values():
        before = _existing_account(db, row["slowbooks_account"])
        accounts[row["slowbooks_account"].lower()] = _account(
            db, row["slowbooks_account"], row["new_account_type"]
        )
        if before is None:
            made["accounts"] += 1

    def acct(name):
        return accounts.get(name.lower()) or _existing_account(db, name)

    customers = {}
    for source, row in plan.customers.items():
        cust = _named(db, Customer, row["slowbooks_name"])
        if cust is None:
            cust = Customer(
                name=row["slowbooks_name"][:200],
                email=row["email"] or None,
                phone=row["phone"] or None,
                **_address(row["address"]),
            )
            db.add(cust)
            db.flush()
            made["customers"] += 1
        else:
            # a merged customer takes details the first source lacked
            cust.email = cust.email or row["email"] or None
            cust.phone = cust.phone or row["phone"] or None
            if not cust.bill_address1:
                for k, v in _address(row["address"]).items():
                    setattr(cust, k, v)
        customers[source] = cust

    items = {}
    for title, row in plan.items.items():
        item = _named(db, Item, row["slowbooks_item"])
        if item is None:
            income = acct(row["income_account"]) if row["income_account"] else None
            item = Item(
                name=row["slowbooks_item"][:200],
                item_type=ItemType(row["item_type"]),
                rate=money(row["price"]),
                income_account_id=income.id if income else None,
                is_taxable=False,
            )
            db.add(item)
            db.flush()
            made["items"] += 1
        items[title.lower()] = item

    invoices = {}
    for inv in plan.invoices:
        if db.query(Invoice.id).filter(Invoice.invoice_number == inv["number"]).first():
            continue
        cust = customers[inv["customer"]]
        taxable = sum(
            (
                _q(ln["quantity"] * ln["rate"])
                for ln in inv["lines"]
                if ln["is_taxable"]
            ),
            Decimal("0"),
        )
        days = (inv["due_date"] - inv["date"]).days
        invoice = Invoice(
            invoice_number=inv["number"],
            customer_id=cust.id,
            status=InvoiceStatus.SENT,
            date=inv["date"],
            due_date=inv["due_date"],
            terms=f"Net {days}" if days > 0 else "Due on Receipt",
            subtotal=inv["subtotal"],
            # the CRM kept the tax, not the rate: the rate is shown, the tax posts
            tax_rate=(
                (inv["tax"] / taxable).quantize(Decimal("0.000001"))
                if taxable
                else Decimal("0")
            ),
            tax_amount=inv["tax"],
            total=inv["total"],
            amount_paid=Decimal("0"),
            balance_due=inv["total"],
            bill_address1=cust.bill_address1,
            bill_address2=cust.bill_address2,
            bill_city=cust.bill_city,
            bill_state=cust.bill_state,
            bill_zip=cust.bill_zip,
        )
        db.add(invoice)
        db.flush()
        rows = []
        for ln in inv["lines"]:
            item = items.get(ln["title"].lower())
            row = InvoiceLine(
                invoice_id=invoice.id,
                item_id=item.id if item else None,
                description=ln["description"],
                quantity=ln["quantity"],
                rate=ln["rate"],
                amount=_q(ln["quantity"] * ln["rate"]),
                is_taxable=ln["is_taxable"],
                line_order=ln["line_order"],
            )
            db.add(row)
            rows.append(row)
        db.flush()
        txn = _post_invoice_journal(db, invoice, rows, cust.name)
        invoice.transaction_id = txn.id
        invoices[inv["number"]] = invoice
        made["invoices"] += 1

    deposit = acct(plan.accounts[("deposit", DEPOSIT_ROW)]["slowbooks_account"])
    ar_id = get_ar_account_id(db)
    for p in plan.payments:
        invoice = invoices.get(p["invoice"])
        if invoice is None:  # its invoice was already in the books
            continue
        cust = db.get(Customer, invoice.customer_id)
        payment = Payment(
            customer_id=cust.id,
            date=p["date"],
            amount=p["amount"],
            method=p["method"],
            check_number=p["check_number"],
            reference=f"invoices.csv #{invoice.invoice_number}",
            deposit_to_account_id=deposit.id,
            notes=p["notes"],
        )
        db.add(payment)
        db.flush()
        db.add(
            PaymentAllocation(
                payment_id=payment.id, invoice_id=invoice.id, amount=p["amount"]
            )
        )
        invoice.amount_paid += p["amount"]
        invoice.balance_due -= p["amount"]
        invoice.status = (
            InvoiceStatus.PAID if invoice.balance_due == 0 else InvoiceStatus.PARTIAL
        )
        txn = create_journal_entry(
            db,
            p["date"],
            f"Payment from {cust.name}",
            [
                {
                    "account_id": deposit.id,
                    "debit": p["amount"],
                    "credit": Decimal("0"),
                    "description": f"Payment from {cust.name}",
                },
                {
                    "account_id": ar_id,
                    "debit": Decimal("0"),
                    "credit": p["amount"],
                    "description": f"Payment from {cust.name}",
                },
            ],
            source_type="payment",
            source_id=payment.id,
            reference=p["check_number"] or "",
        )
        payment.transaction_id = txn.id
        made["payments"] += 1

    from app.services.sales_tax_agencies import tax_account_ids

    tax_ids = set(tax_account_ids(db))
    for e in plan.expenses:
        if _already_posted(db, e["ref"]):
            continue
        row = plan.accounts[("expense", e["category"])]
        to, paid_from = acct(row["slowbooks_account"]), acct(row["paid_from"])
        # sales tax sent to the state or parish: a payment of what's owed,
        # which the Sales Tax report counts as paid, not an expense
        tax_paid = to.id in tax_ids
        vendor = _named(db, Vendor, e["payee"]) if e["payee"] else None
        if vendor is None and e["payee"]:
            vendor = Vendor(name=e["payee"][:200])
            db.add(vendor)
            db.flush()
            made["vendors"] += 1
        desc = e["memo"] or e["payee"]
        create_journal_entry(
            db,
            e["date"],
            (
                f"Sales Tax Payment: {to.name}"
                if tax_paid
                else f"Expense: {e['payee']}" if e["payee"] else "Expense"
            ),
            [
                {
                    "account_id": to.id,
                    "debit": e["amount"],
                    "credit": Decimal("0"),
                    "description": desc,
                },
                {
                    "account_id": paid_from.id,
                    "debit": Decimal("0"),
                    "credit": e["amount"],
                    "description": desc,
                },
            ],
            source_type="sales_tax_payment" if tax_paid else "expense",
            source_id=None if tax_paid else vendor.id if vendor else None,
            reference=e["ref"],
        )
        made["sales tax payments" if tax_paid else "expenses"] += 1

    for t in plan.transfers:
        if _already_posted(db, t["ref"]):
            continue
        frm = acct(plan.accounts[("transfer", t["from"])]["slowbooks_account"])
        to = acct(plan.accounts[("transfer", t["to"])]["slowbooks_account"])
        if frm.bank_kind and to.bank_kind:
            post_transfer(
                db, t["date"], frm, to, t["amount"], memo=t["memo"], reference=t["ref"]
            )
        else:
            # owner's draw or contribution: not a bank-to-bank transfer
            create_journal_entry(
                db,
                t["date"],
                f"{t['from']} → {t['to']}" + (f" — {t['memo']}" if t["memo"] else ""),
                [
                    {"account_id": to.id, "debit": t["amount"], "credit": Decimal("0")},
                    {
                        "account_id": frm.id,
                        "debit": Decimal("0"),
                        "credit": t["amount"],
                    },
                ],
                source_type="manual",
                reference=t["ref"],
            )
        made["transfers"] += 1
    db.flush()
    return dict(made)


def true_up_bank(db: Session, plan: Plan, balance: Decimal, as_of: date):
    """Bring the account customer payments went to up (or down) to what the
    bank says on ``as_of``. The CRM didn't hold every expense, so its
    history alone leaves the account wrong; the difference posts against
    3900 Opening Balance Equity, as a bank feed's statement balance does.
    Returns the posting, or None when the books already agree."""
    from app.services.bank_posting import post_statement_balance

    name = plan.accounts[("deposit", DEPOSIT_ROW)]["slowbooks_account"]
    txn = post_statement_balance(
        db, _existing_account(db, name), as_of, balance, post_difference=True
    )
    db.flush()
    return txn


def set_tax_agencies(db: Session, specs: list[str]) -> list[dict]:
    """``["Louisiana State=5", "Jefferson Parish=5.25"]`` -> the company's
    sales tax agencies, each with a "Sales Tax Payable – <name>" account
    (found by name, else made). Run before apply_plan so the import's tax
    splits between them. Doesn't commit."""
    from app.services.accounting import ensure_account
    from app.services.sales_tax_agencies import save_agencies

    rows = []
    for spec in specs:
        name, _, rate = spec.rpartition("=")
        name = name.strip()
        if not name or not rate.strip():
            raise ValueError(
                f"--tax-agency {spec!r}: write it as NAME=RATE, e.g. 'Louisiana State=5'."
            )
        acct = ensure_account(
            db, None, f"Sales Tax Payable – {name}"[:200], AccountType.LIABILITY
        )
        rows.append(
            {"name": name, "rate": Decimal(rate.strip()), "account_id": acct.id}
        )
    save_agencies(db, rows)
    db.flush()
    return rows
