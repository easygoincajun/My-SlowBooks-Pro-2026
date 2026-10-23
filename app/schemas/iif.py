from pydantic import BaseModel


class IIFImportResult(BaseModel):
    classes: int = 0
    accounts: int = 0
    customers: int = 0
    vendors: int = 0
    items: int = 0
    invoices: int = 0
    payments: int = 0
    sales_receipts: int = 0
    estimates: int = 0
    bills: int = 0
    deposits: int = 0
    # checks, card charges, transfers, bill payments and journal entries
    # (#344), and the GENERAL JOURNAL ones of those
    ledger_entries: int = 0
    journal_entries: int = 0
    duplicates_skipped: int = 0
    # #288: a mixed file reconciles — parsed blocks = imported + duplicates
    # + unsupported + failed; the unsupported ones are named
    transaction_blocks: int = 0
    unsupported: int = 0
    unsupported_blocks: list[dict] = []
    # ALL-CAPS names rewritten on request ("Change ALL-CAPS names", #195)
    names_changed: int = 0
    errors: list[dict] = []
    warnings: list[str] = []


class IIFValidationReport(BaseModel):
    valid: bool
    sections_found: list[str] = []
    record_counts: dict = {}
    warnings: list[str] = []
    errors: list[str] = []
    # How many customer, vendor and account names are in ALL CAPS, and a few
    # of them as "Change ALL-CAPS names" would import them.
    caps_names: int = 0
    caps_name_examples: list[dict] = []
    unsupported: list[dict] = []  # #288: the blocks the import would not take
    unsupported_counts: dict = {}
