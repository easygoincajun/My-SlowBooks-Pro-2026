"""QuickBooks' "<Parent> - Other" sections post to the parent account
(discussion #349: a 16-year export was refused as "accounts not in the
chart" for every parent that has sub-accounts)."""

from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction, TransactionLine
from app.services.qb_ledger_import import SOURCE_TYPE, _parent_of_other_section


def _chart(db):
    office = Account(name="Office Expense", account_type=AccountType.EXPENSE)
    db.add(office)
    db.flush()
    db.add(
        Account(name="Software", account_type=AccountType.EXPENSE, parent_id=office.id)
    )
    db.commit()


def _report(label_columns: int) -> bytes:
    """A ledger export in the account-grouped layout with ``label_columns``
    columns before Type: one (the sub-account headings stand by their names)
    or two (each heading in its depth's column, as QuickBooks writes it)."""
    pad = "," * label_columns

    def heading(text, depth=0):
        return "," * depth + text + "," * (label_columns - depth) + ",,,,,,,,"

    def total(text, depth, debit, credit):
        # a Total row carries the section's sums in the Debit and Credit columns
        cells = [""] * (label_columns + 9)
        cells[depth] = text
        cells[label_columns + 6] = debit
        cells[label_columns + 7] = credit
        return ",".join(cells)

    def row(ttype, date, num, name, memo, split, debit, credit):
        return f"{pad}{ttype},{date},{num},{name},{memo},{split},{debit},{credit},"

    sub = min(1, label_columns - 1)
    lines = [
        pad + "Type,Date,Num,Name,Memo,Split,Debit,Credit,Balance",
        heading("Checking"),
        row(
            "Check",
            "01/05/2026",
            "101",
            "Acme Office",
            "toner",
            "Office Expense",
            "",
            "60.00",
        ),
        row(
            "Check",
            "01/06/2026",
            "102",
            "Soft Co",
            "licence",
            "Office Expense:Software",
            "",
            "40.00",
        ),
        total("Total Checking", 0, "0.00", "100.00"),
        heading("Office Expense"),
        heading("Office Expense - Other", sub),
        row(
            "Check",
            "01/05/2026",
            "101",
            "Acme Office",
            "toner",
            "Checking",
            "60.00",
            "",
        ),
        total("Total Office Expense - Other", sub, "60.00", "0.00"),
        heading("Software", sub),
        row(
            "Check", "01/06/2026", "102", "Soft Co", "licence", "Checking", "40.00", ""
        ),
        total("Total Software", sub, "40.00", "0.00"),
        total("Total Office Expense", 0, "100.00", "0.00"),
        total("TOTAL", 0, "100.00", "100.00"),
    ]
    return ("\n".join(lines) + "\n").encode("cp1252")


def _upload(client, data: bytes):
    r = client.post(
        "/api/csv/import/qb-report",
        files={"file": ("Transaction_Detail_by_Account.csv", data, "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _posted(db):
    txns = (
        db.execute(select(Transaction).where(Transaction.source_type == SOURCE_TYPE))
        .scalars()
        .all()
    )
    out = {}
    for t in txns:
        for ln in db.execute(
            select(TransactionLine).where(TransactionLine.transaction_id == t.id)
        ).scalars():
            acct = db.get(Account, ln.account_id)
            out.setdefault(acct.name, 0)
            out[acct.name] += float(ln.debit or 0) - float(ln.credit or 0)
    return out


def test_the_others_section_posts_to_the_parent_two_label_columns(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, _report(2))
    assert data["detected"] == "ledger"
    assert not data["errors"], data["errors"]
    assert data["journals"] == 2
    posted = _posted(db_session)
    assert posted["Office Expense"] == 60.0  # the parent's own posting
    assert posted["Software"] == 40.0
    assert posted["Checking"] == -100.0


def test_the_others_section_posts_to_the_parent_one_label_column(
    client, db_session, seed_accounts
):
    _chart(db_session)
    data = _upload(client, _report(1))
    assert not data["errors"], data["errors"]
    assert data["journals"] == 2
    posted = _posted(db_session)
    assert posted["Office Expense"] == 60.0 and posted["Software"] == 40.0


def test_what_the_lookup_takes_and_leaves(db_session, seed_accounts):
    _chart(db_session)
    found = _parent_of_other_section(
        db_session, "Office Expense:Office Expense - Other"
    )
    assert found is not None and found.name == "Office Expense"
    found = _parent_of_other_section(db_session, "Office Expense - Other")
    assert found is not None and found.name == "Office Expense"
    assert _parent_of_other_section(db_session, "Office Expense:Software") is None
    assert _parent_of_other_section(db_session, "Nowhere - Other") is None
    assert _parent_of_other_section(db_session, " - Other") is None
