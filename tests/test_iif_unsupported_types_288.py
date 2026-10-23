"""An IIF block of a type the importer doesn't take is named, not dropped
(#288). The dispatch covers INVOICE, PAYMENT, CASH SALE, ESTIMATE, BILL and
DEPOSIT, and since 2.24.0 CHECK, CREDIT CARD, TRANSFER, BILLPMT and GENERAL
JOURNAL (#344); a block of any other type (a CREDIT MEMO, a PURCHORD) used
to be skipped without a word, so a mixed file imported its invoices and lost
the rest silently. Validate names the type and the blocks; the import counts
them with their identity, and every parsed block is accounted for.
"""

from pathlib import Path

from app.services.iif_import import SUPPORTED_TRNS_TYPES, import_all, validate_iif

ROOT = Path(__file__).resolve().parents[1]

HEADER = (
    "!TRNS\tTRNSID\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO\n"
    "!SPL\tSPLID\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO\n"
    "!ENDTRNS\n"
)
INVOICE = (
    "TRNS\t\tINVOICE\t03/10/2026\tAccounts Receivable\tAcme Corp\t500.00\t1001\t\n"
    "SPL\t\tINVOICE\t03/10/2026\tService Income\tAcme Corp\t-500.00\t1001\t\n"
    "ENDTRNS\n"
)
JOURNAL = (  # a CREDIT MEMO: a type the importer does not take
    "TRNS\t\tCREDIT MEMO\t03/12/2026\tAccounts Receivable\tAcme Corp\t-48.00\tGJ-7\tReturned part\n"
    "SPL\t\tCREDIT MEMO\t03/12/2026\tService Income\tAcme Corp\t48.00\tGJ-7\t\n"
    "ENDTRNS\n"
)
CHECK = (  # a PURCHORD: another
    "TRNS\t\tPURCHORD\t03/13/2026\tPurchase Orders\tCorner Hardware\t-120.00\t2041\t\n"
    "SPL\t\tPURCHORD\t03/13/2026\tAuto & Truck Expense\tCorner Hardware\t120.00\t2041\t\n"
    "ENDTRNS\n"
)


def _customer(client):
    r = client.post("/api/customers", json={"name": "Acme Corp"})
    assert r.status_code == 201, r.text


def test_the_dispatch_and_the_supported_set_agree():
    src = (ROOT / "app" / "services" / "iif_import.py").read_text(encoding="utf-8")
    for ttype in ("INVOICE", "PAYMENT", "ESTIMATE", "BILL", "DEPOSIT"):
        assert f'trns_type == "{ttype}"' in src and ttype in SUPPORTED_TRNS_TYPES
    assert {"CASH SALE", "CASHSALE", "SALES RECEIPT"} <= SUPPORTED_TRNS_TYPES
    # the ledger blocks (#344) dispatch as one family
    from app.services.iif_import import _LEDGER_BLOCK_SOURCES

    for ttype in ("GENERAL JOURNAL", "CHECK", "CREDIT CARD", "TRANSFER", "BILLPMT"):
        assert ttype in _LEDGER_BLOCK_SOURCES and ttype in SUPPORTED_TRNS_TYPES
    assert "CREDIT MEMO" not in SUPPORTED_TRNS_TYPES
    assert "PURCHORD" not in SUPPORTED_TRNS_TYPES


def test_validate_names_the_unsupported_blocks_and_stays_valid():
    report = validate_iif(HEADER + INVOICE + JOURNAL + CHECK + JOURNAL)
    assert report["valid"] is True
    assert report["record_counts"]["TRNS"] == 4
    assert report["unsupported_counts"] == {"CREDIT MEMO": 2, "PURCHORD": 1}
    assert [(u["block"], u["type"], u["docnum"]) for u in report["unsupported"]] == [
        (2, "CREDIT MEMO", "GJ-7"),
        (3, "PURCHORD", "2041"),
        (4, "CREDIT MEMO", "GJ-7"),
    ]
    assert report["unsupported"][1]["name"] == "Corner Hardware"
    assert report["unsupported"][0]["date"] == "03/12/2026"
    gj = [
        w
        for w in report["warnings"]
        if w.startswith("2 CREDIT MEMO blocks not imported")
    ]
    assert gj and "(blocks 2, 4)" in gj[0] and "BILLPMT and GENERAL JOURNAL" in gj[0]
    assert any(
        w.startswith("1 PURCHORD block not imported (block 3)")
        for w in report["warnings"]
    )


def test_a_mixed_file_imports_what_it_can_and_accounts_for_the_rest(
    client, db_session, seed_accounts
):
    _customer(client)
    result = import_all(db_session, HEADER + INVOICE + JOURNAL + CHECK)
    assert result["invoices"] == 1
    assert result["transaction_blocks"] == 3
    assert result["unsupported"] == 2
    assert [(u["block"], u["type"]) for u in result["unsupported_blocks"]] == [
        (2, "CREDIT MEMO"),
        (3, "PURCHORD"),
    ]
    assert result["errors"] == []
    assert result["duplicates_skipped"] == 0
    assert (
        result["invoices"]
        + result["duplicates_skipped"]
        + result["unsupported"]
        + len(result["errors"])
        == result["transaction_blocks"]
    )
    assert any("CREDIT MEMO block not imported" in w for w in result["warnings"])
    # nothing of the journal or the check landed
    from app.models.transactions import Transaction

    assert db_session.query(Transaction).filter_by(source_type="invoice").count() == 1
    assert db_session.query(Transaction).count() == 1


def test_an_unsupported_only_file_imports_nothing_and_says_so(
    db_session, seed_accounts
):
    result = import_all(db_session, HEADER + JOURNAL)
    assert result["invoices"] == 0 and result["unsupported"] == 1
    assert result["transaction_blocks"] == 1 and result["errors"] == []
    assert result["unsupported_blocks"][0]["docnum"] == "GJ-7"


def test_a_reimport_keeps_duplicates_and_unsupported_apart(
    client, db_session, seed_accounts
):
    _customer(client)
    import_all(db_session, HEADER + INVOICE + JOURNAL)
    again = import_all(db_session, HEADER + INVOICE + JOURNAL)
    assert again["invoices"] == 0 and again["duplicates_skipped"] == 1
    assert again["unsupported"] == 1 and again["transaction_blocks"] == 2


def test_the_page_shows_the_count_and_the_reconciliation():
    js = (ROOT / "app" / "static" / "js" / "iif.js").read_text(encoding="utf-8")
    assert "report.unsupported_counts" in js  # Validate, by type
    assert "result.unsupported" in js  # the import's row
    assert "result.transaction_blocks" in js  # every block accounted for
    # as one sentence on a line of its own, not a label beside a count that
    # wrapped after "the" with the count reading as a superscript (2.23.1
    # gate, NEW-106); and the toast counts what the panel counts
    assert (
        '<div class="result-row result-row--line">${n} transaction block'
        "${n === 1 ? '' : 's'} in the file: ${imported} imported, " in js
    )
    css = (ROOT / "app" / "static" / "css" / "style.css").read_text(encoding="utf-8")
    assert ".iif-results .result-row--line {\n    display: block;\n}" in css
    assert "IIFPage._importedWords(result)" in js
