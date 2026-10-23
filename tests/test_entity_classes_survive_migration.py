"""A QuickBooks file that keeps its entities apart by class (a nonprofit and
two LLCs in one file) must arrive with every posting still coded to its
entity. Three ways it does not today: the IIF import drops CLASS on invoices,
sales receipts and estimates (it reads it for bills and deposits only); the
report CSV import never reads the Class column at all; and the IIF export
writes the document's header class on every split row, so a document whose
lines belong to two entities leaves with all of them under one.
"""

import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _post(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 201, (path, r.text)
    return r.json()


def _import_iif(client, text):
    r = client.post(
        "/api/iif/import",
        files={
            "file": ("entities.iif", io.BytesIO(text.encode("utf-8")), "text/plain")
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


IIF = """!CLASS\tNAME\tHIDDEN
CLASS\tNorth LLC\tN
CLASS\tSouth LLC\tN
!TRNS\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tDUEDATE\tTERMS\tMEMO\tCLASS
!SPL\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tDUEDATE\tTERMS\tMEMO\tCLASS\tINVITEM\tQNTY\tPRICE\tTAXABLE
!ENDTRNS
TRNS\tINVOICE\t09/10/2026\tAccounts Receivable\tAlex Vega\t500.00\t7001\t10/10/2026\tNet 30\t\tNorth LLC
SPL\tINVOICE\t09/10/2026\tService Income\tAlex Vega\t-500.00\t7001\t\t\tNorth work\tNorth LLC\t\t-1\t500\tN
ENDTRNS
TRNS\tBILL\t09/11/2026\tAccounts Payable\tPier Lumber\t-300.00\tL-7\t10/11/2026\tNet 30\t\tSouth LLC
SPL\tBILL\t09/11/2026\t{expense}\tPier Lumber\t300.00\tL-7\t\t\tSouth lumber\tSouth LLC
ENDTRNS
"""


def test_the_iif_import_codes_an_invoice_to_its_class_as_it_does_a_bill(
    client, db_session, seed_accounts
):
    from app.models.bills import Bill
    from app.models.classes import TxnClass
    from app.models.invoices import Invoice

    _post(client, "/api/customers", {"name": "Alex Vega"})
    _post(client, "/api/vendors", {"name": "Pier Lumber"})
    result = _import_iif(client, IIF.replace("{expense}", seed_accounts["6000"].name))
    assert result["errors"] == [], result["errors"]
    assert result["invoices"] == 1 and result["bills"] == 1, result
    north = db_session.query(TxnClass).filter_by(name="North LLC").one()
    south = db_session.query(TxnClass).filter_by(name="South LLC").one()
    bill = db_session.query(Bill).filter_by(bill_number="L-7").one()
    assert bill.class_id == south.id  # bills keep their class
    inv = db_session.query(Invoice).filter_by(invoice_number="7001").one()
    assert inv.class_id == north.id, "the invoice arrived with no class"
    assert [ln.class_id for ln in inv.lines] == [north.id]
    # and its posting is coded to the entity, not Uncategorized
    posted = {(ln.account_id, ln.class_id) for ln in inv.transaction.lines}
    assert all(c == north.id for _, c in posted), posted


def test_the_report_csv_import_reads_the_class_column(
    client, db_session, seed_accounts
):
    from app.models.classes import TxnClass
    from app.models.invoices import Invoice
    from app.services.qb_report_import import import_invoice_report

    fixture = (ROOT / "tests/fixtures/qb_transaction_detail_invoices.csv").read_text(
        encoding="utf-8"
    )
    # the report's Class column, empty in the fixture, filled the way QuickBooks
    # fills it: on the header row and on every line
    filled = (
        fixture.replace(
            ",04/12/26,1031,Alex Vega:Kitchen remodel,,,,Accounts Receivable,,,-SPLIT-",
            ",04/12/26,1031,Alex Vega:Kitchen remodel,,,,Accounts Receivable,North LLC,,-SPLIT-",
        )
        .replace(
            ",Exterior door,,Service Income,,,Accounts Receivable,-3,",
            ",Exterior door,,Service Income,North LLC,,Accounts Receivable,-3,",
        )
        .replace(
            ",Install labor,,Service Income,,,Accounts Receivable,-1.5,",
            ",Install labor,,Service Income,North LLC,,Accounts Receivable,-1.5,",
        )
    )
    assert filled.count("North LLC") == 3, filled.count("North LLC")
    _post(client, "/api/classes", {"name": "North LLC"})
    result = import_invoice_report(db_session, filled)
    assert result["imported"] == 3, result
    north = db_session.query(TxnClass).filter_by(name="North LLC").one()
    inv = db_session.query(Invoice).filter_by(invoice_number="1031").one()
    assert inv.class_id == north.id, "the report's Class column was ignored"
    assert {ln.class_id for ln in inv.lines} == {north.id}


def test_the_export_keeps_each_lines_own_class(
    client, db_session, seed_accounts, seed_customer
):
    north = _post(client, "/api/classes", {"name": "North LLC"})
    south = _post(client, "/api/classes", {"name": "South LLC"})
    inv = _post(
        client,
        "/api/invoices",
        {
            "customer_id": seed_customer.id,
            "date": "2026-09-12",
            "tax_rate": "0",
            "class_id": north["id"],
            "lines": [
                {
                    "description": "North work",
                    "quantity": "1",
                    "rate": "100.00",
                    "class_id": north["id"],
                },
                {
                    "description": "South work",
                    "quantity": "1",
                    "rate": "200.00",
                    "class_id": south["id"],
                },
            ],
        },
    )
    text = client.get("/api/iif/export/invoices").text.replace("\r\n", "\n")
    rows = [
        ln.split("\t")
        for ln in text.split("\n")
        if ln.startswith("SPL\t") and f"\t{inv['invoice_number']}\t" in ln
    ]
    header = next(ln for ln in text.split("\n") if ln.startswith("!SPL\t")).split("\t")
    c = header.index("CLASS")
    assert [r[c] for r in rows] == ["North LLC", "South LLC"], [r[c] for r in rows]


IIF_MIXED = """!TRNS\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tDUEDATE\tTERMS\tMEMO\tCLASS
!SPL\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tDUEDATE\tTERMS\tMEMO\tCLASS\tINVITEM\tQNTY\tPRICE\tTAXABLE
!ENDTRNS
TRNS\tBILL\t09/11/2026\tAccounts Payable\tPier Lumber\t-500.00\tL-9\t10/11/2026\tNet 30\t\t
SPL\tBILL\t09/11/2026\t{expense}\tPier Lumber\t300.00\tL-9\t\t\tNorth lumber\tNorth LLC
SPL\tBILL\t09/11/2026\t{expense}\tPier Lumber\t200.00\tL-9\t\t\tSouth lumber\tSouth LLC
ENDTRNS
TRNS\tDEPOSIT\t09/12/2026\t{bank}\t\t150.00\tDEP-9\t\t\tTwo entities\t
SPL\tDEPOSIT\t09/12/2026\tService Income\t\t-100.00\tDEP-9\t\t\tNorth cash\tNorth LLC
SPL\tDEPOSIT\t09/12/2026\tService Income\t\t-50.00\tDEP-9\t\t\tSouth cash\tSouth LLC
ENDTRNS
"""


def test_a_block_whose_lines_belong_to_two_entities_keeps_each_line_under_its_own(
    client, db_session, seed_accounts
):
    """The import refused a bill or deposit whose split rows carried two
    classes; each line keeps its own now, as the forms allow since 2.22.0,
    and a class the file names is created on first sight with a note."""
    from app.models.bills import Bill
    from app.models.classes import TxnClass
    from app.models.transactions import Transaction

    _post(client, "/api/vendors", {"name": "Pier Lumber"})
    text = IIF_MIXED.replace("{expense}", seed_accounts["6000"].name).replace(
        "{bank}", seed_accounts["1000"].name
    )
    result = _import_iif(client, text)
    assert result["errors"] == [], result["errors"]
    assert result["bills"] == 1 and result["deposits"] == 1, result
    assert sorted(w for w in result["warnings"] if "created" in w) == [
        "Class 'North LLC' is named in the file and was not in your books: created",
        "Class 'South LLC' is named in the file and was not in your books: created",
    ]
    north = db_session.query(TxnClass).filter_by(name="North LLC").one()
    south = db_session.query(TxnClass).filter_by(name="South LLC").one()
    bill = db_session.query(Bill).filter_by(bill_number="L-9").one()
    assert bill.class_id is None  # no class shared by every line, none on the header
    assert sorted(ln.class_id for ln in bill.lines) == sorted([north.id, south.id])
    posted = {
        (float(ln.debit), ln.class_id) for ln in bill.transaction.lines if ln.debit
    }
    assert posted == {(300.0, north.id), (200.0, south.id)}
    dep = db_session.query(Transaction).filter_by(reference="DEP-9").one()
    credits = {(float(ln.credit), ln.class_id) for ln in dep.lines if ln.credit}
    assert credits == {(100.0, north.id), (50.0, south.id)}


def test_a_mixed_class_invoice_round_trips_with_both_classes(
    client, db_session, seed_accounts, seed_customer
):
    from app.models.invoices import Invoice

    north = _post(client, "/api/classes", {"name": "North LLC"})
    south = _post(client, "/api/classes", {"name": "South LLC"})
    inv = _post(
        client,
        "/api/invoices",
        {
            "customer_id": seed_customer.id,
            "date": "2026-09-12",
            "tax_rate": "0",
            "lines": [
                {
                    "description": "North work",
                    "quantity": "1",
                    "rate": "100.00",
                    "class_id": north["id"],
                },
                {
                    "description": "South work",
                    "quantity": "1",
                    "rate": "200.00",
                    "class_id": south["id"],
                },
            ],
        },
    )
    text = client.get("/api/iif/export/invoices").text
    again = text.replace(f"\t{inv['invoice_number']}\t", "\tRT-308\t")
    result = _import_iif(client, again)
    assert result["invoices"] == 1 and result["errors"] == [], result
    db_session.expire_all()
    back = db_session.query(Invoice).filter_by(invoice_number="RT-308").one()
    assert (
        back.class_id is None
    )  # the header carried none: two entities on one document
    lines = sorted(back.lines, key=lambda ln: float(ln.rate))
    assert [ln.class_id for ln in lines] == [north["id"], south["id"]]
    posted = {
        (float(ln.credit), ln.class_id) for ln in back.transaction.lines if ln.credit
    }
    assert posted == {(100.0, north["id"]), (200.0, south["id"])}


def test_the_export_falls_back_to_the_documents_class_for_a_line_without_one(
    client, seed_accounts, seed_customer
):
    north = _post(client, "/api/classes", {"name": "North LLC"})
    inv = _post(
        client,
        "/api/invoices",
        {
            "customer_id": seed_customer.id,
            "date": "2026-09-12",
            "tax_rate": "0",
            "class_id": north["id"],
            "lines": [{"description": "Work", "quantity": "1", "rate": "100.00"}],
        },
    )
    text = client.get("/api/iif/export/invoices").text.replace("\r\n", "\n")
    header = next(ln for ln in text.split("\n") if ln.startswith("!SPL\t")).split("\t")
    rows = [
        ln.split("\t")
        for ln in text.split("\n")
        if ln.startswith("SPL\t") and f"\t{inv['invoice_number']}\t" in ln
    ]
    assert [r[header.index("CLASS")] for r in rows] == ["North LLC"]


def test_the_report_import_creates_a_named_class_once_and_says_so(
    client, db_session, seed_accounts
):
    from app.models.classes import TxnClass
    from app.services.qb_report_import import import_invoice_report

    fixture = (ROOT / "tests/fixtures/qb_transaction_detail_invoices.csv").read_text(
        encoding="utf-8"
    )
    filled = fixture.replace(
        ",Service Income,,,Accounts Receivable,",
        ",Service Income,Rental LLC,,Accounts Receivable,",
    )
    assert filled.count("Rental LLC") >= 3
    result = import_invoice_report(db_session, filled)
    assert result["imported"] == 3, result
    assert (
        result["warnings"].count(
            "Class 'Rental LLC' is named in the report and was not in your books: created"
        )
        == 1
    )
    assert db_session.query(TxnClass).filter_by(name="Rental LLC").count() == 1


def test_the_deposit_report_reads_a_class_column_when_the_report_has_one(
    client, db_session, seed_accounts
):
    from app.models.classes import TxnClass
    from app.models.transactions import Transaction
    from app.services.qb_report_import import import_deposit_report

    fixture = (ROOT / "tests/fixtures/qb_deposit_detail.csv").read_text(
        encoding="utf-8"
    )
    # add a Class column at the end of every row: blank on rails, named on details
    out = []
    for raw in fixture.splitlines():
        if raw.startswith(",Type,Date,Num,Name,Memo,Account,Pay Meth,Amount"):
            out.append(raw + ",Class")
        elif (
            raw.startswith(",")
            and not raw.startswith((",Deposit,", ",Type,"))
            and raw.count(",") >= 8
            and raw.split(",")[6].strip()
        ):
            out.append(
                raw + ",North LLC"
            )  # a detail row: Type, then the paid-in account
        else:
            out.append(raw + ",")
    text = "\n".join(out) + "\n"
    before = db_session.query(Transaction).count()
    result = import_deposit_report(db_session, text)
    assert result["deposits"] >= 1, result
    north = db_session.query(TxnClass).filter_by(name="North LLC").one()
    new = db_session.query(Transaction).filter(Transaction.id > 0).all()[before:]
    credit_classes = {ln.class_id for t in new for ln in t.lines if ln.credit}
    assert credit_classes == {north.id}, credit_classes


def test_the_deposit_export_writes_each_credits_own_class_and_the_memo_as_typed(
    client, db_session, seed_accounts
):
    """The fifth writer (deposits) still wrote the deposit's class on every
    split row, so DEP-9's two credits left with no class (2.23.1 gate,
    NEW-96); and the import's "IIF Import — " prefix went out in the MEMO,
    so each round trip added another (NEW-100)."""
    from app.models.transactions import Transaction

    _post(client, "/api/vendors", {"name": "Pier Lumber"})
    text = IIF_MIXED.replace("{expense}", seed_accounts["6000"].name).replace(
        "{bank}", seed_accounts["1000"].name
    )
    assert _import_iif(client, text)["deposits"] == 1
    out = client.get("/api/iif/export/deposits").text.replace("\r\n", "\n")
    header = next(ln for ln in out.split("\n") if ln.startswith("!SPL\t")).split("\t")
    ci, mi = header.index("CLASS"), header.index("MEMO")
    spl = [
        ln.split("\t")
        for ln in out.split("\n")
        if ln.startswith("SPL\t") and "\tDEP-9\t" in ln
    ]
    trns = [
        ln.split("\t")
        for ln in out.split("\n")
        if ln.startswith("TRNS\t") and "\tDEP-9\t" in ln
    ]
    assert sorted(r[ci] for r in spl) == ["North LLC", "South LLC"], [
        r[ci] for r in spl
    ]
    assert trns[0][header.index("MEMO")] == "Two entities", trns[0]
    # the round trip keeps both classes and the memo as typed
    again = out.replace("\tDEP-9\t", "\tDEP-9B\t")
    assert _import_iif(client, again)["deposits"] == 1
    dep = db_session.query(Transaction).filter_by(reference="DEP-9B").one()
    assert sorted(ln.class_id for ln in dep.lines if ln.credit) == sorted(
        ln.class_id
        for ln in db_session.query(Transaction).filter_by(reference="DEP-9").one().lines
        if ln.credit
    )
    out2 = client.get("/api/iif/export/deposits").text.replace("\r\n", "\n")
    trns2 = [
        ln.split("\t")
        for ln in out2.split("\n")
        if ln.startswith("TRNS\t") and "\tDEP-9B\t" in ln
    ]
    assert trns2[0][mi] == "Two entities", trns2[0][mi]
