"""Documents an earlier QuickBooks Online import created, acted on here.

The QuickBooks Online connector was retired in 2.23.2 (services/qbo_common.py
says why). While it existed, its import brought invoices, sales receipts and
payments in as documents without a posting of their own, and its ledger
import posted QBO's own lines for each one, as a journal mapped by its QBO
transaction ("Invoice:130", "Sales Receipt:132", "Payment:131"). A company
that was connected still has those documents, and a person here voids or
edits one like any other:

- its import posting is reversed with it (the usual reversing entry, keyed
  by the transaction it reverses);
- an edit makes it an ordinary document of ours, with a posting of its own
  (the import's one reversed first, so A/R is never counted twice);
- the import mappings are marked CHANGED_HERE, as they were when a later
  import had to leave the document alone.

For any other document every function here is a no-op.
"""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.invoices import Invoice, InvoiceStatus
from app.models.items import InventoryMovement, Item, MovementType
from app.models.payments import Payment
from app.models.qbo_mapping import QBOMapping
from app.models.transactions import Transaction
from app.services.qbo_common import CHANGED_HERE, HERE, ledger_posting

# The QBO transaction type the ledger import posts each document kind under.
LEDGER_TYPE = {
    "invoice": "Invoice",
    "sales_receipt": "Sales Receipt",
    "payment": "Payment",
}
# Reversals keyed by the id of the transaction they reverse (the journal's
# own void, the house void_document() convention, a QBO import posting's).
REVERSALS = (
    "manual_void",
    "bank_entry_void",
    "deposit_void",
    "qbo_ledger_void",
    "qbo_journal_void",
)


def origin(db: Session, kinds, slowbooks_id) -> QBOMapping | None:
    """The QBO mapping of a local document, if it has one."""
    return (
        db.query(QBOMapping)
        .filter(
            QBOMapping.entity_type.in_(kinds),
            QBOMapping.slowbooks_id == slowbooks_id,
        )
        .first()
    )


def invoice_origin(db: Session, invoice) -> QBOMapping | None:
    """The mapping of an invoice or sales receipt the QBO import created
    (no posting of its own); None for one written here, even if exported."""
    if invoice.transaction_id is not None:
        return None
    return origin(db, ("invoice", "sales_receipt"), invoice.id)


def sales_receipt_of(db: Session, payment) -> Invoice | None:
    """The QBO sales receipt a payment is the payment half of (the import
    maps the receipt, not its payment)."""
    for alloc in payment.allocations:
        receipt = alloc.invoice
        if (
            receipt is not None
            and receipt.transaction_id is None
            and origin(db, ("sales_receipt",), receipt.id) is not None
        ):
            return receipt
    return None


def payment_origin(db: Session, payment) -> QBOMapping | None:
    """The mapping of a payment the QBO import created: its own, or its
    sales receipt's."""
    if payment.transaction_id is not None:
        return None
    mapping = origin(db, ("payment",), payment.id)
    if mapping is not None:
        return mapping
    receipt = sales_receipt_of(db, payment)
    return origin(db, ("sales_receipt",), receipt.id) if receipt else None


def reversed_already(db: Session, txn) -> bool:
    return (
        db.query(Transaction.id)
        .filter(
            Transaction.source_type.in_(REVERSALS),
            Transaction.source_id == txn.id,
        )
        .first()
        is not None
    )


def import_posting(db: Session, mapping: QBOMapping | None):
    """(ledger mapping, posting) the ledger import made for a QBO document,
    while that posting stands; None when the ledger import never posted it
    or it has been reversed."""
    if mapping is None or mapping.entity_type not in LEDGER_TYPE:
        return None
    ledger = ledger_posting(db, LEDGER_TYPE[mapping.entity_type], mapping.qbo_id)
    if ledger is None:
        return None
    txn = db.get(Transaction, ledger.slowbooks_id)
    if txn is None or reversed_already(db, txn):
        return None
    return ledger, txn


def mark_changed_here(db: Session, *mappings) -> None:
    """A later import leaves these alone (qbo_common.CHANGED_HERE). One an
    import has kept already stays KEPT_HERE: its line in the log was given
    the first time, and later runs count it with the others."""
    for mapping in mappings:
        if mapping is not None and mapping.qbo_sync_token not in HERE:
            mapping.qbo_sync_token = CHANGED_HERE


def reverse_import_posting(db: Session, txn, *, refusal: str):
    """Reverse an import posting with the usual reversing entry. `refusal`
    is the sentence for a posting on a reconciled bank statement, which
    stays as it is (a reconciled entry cannot be voided)."""
    from app.services.accounting import create_journal_entry, reversing_lines
    from app.services.bank_posting import release_statement_links

    if any(line.reconciliation_id for line in txn.lines):
        raise HTTPException(status_code=400, detail=refusal)
    reversal = create_journal_entry(
        db,
        txn.date,
        f"VOID {txn.description or ''}".strip(),
        reversing_lines(txn.lines),
        source_type=f"{txn.source_type}_void",
        source_id=txn.id,
        reference=txn.reference or "",
        class_id=txn.class_id,
        job_id=txn.job_id,
    )
    release_statement_links(db, txn)
    return reversal


# ---------------------------------------------------------------------------
# Stock a QBO document moved
# ---------------------------------------------------------------------------


def local_cost_live(db: Session, invoice) -> bool:
    """Does a local cost-of-goods entry stand for this QBO document's stock?

    The document import costs a sale here only while the ledger import has
    not posted it; once it has, QBO's posting carries the cost and the local
    entry is reversed (qbo_ledger_import._replace_import_cogs)."""
    for cost in db.query(Transaction).filter(
        Transaction.source_type.in_(("invoice", "invoice_edit")),
        Transaction.source_id == invoice.id,
    ):
        taken_back = (
            db.query(Transaction.id)
            .filter(
                Transaction.source_type == "qbo_cogs_void",
                Transaction.source_id == cost.id,
            )
            .first()
        )
        if cost.lines and not taken_back:
            return True
    return False


def restore_local_cost(db: Session, invoice) -> None:
    """Cost the stock a QBO document moved at the cost it moved at, for a
    document that becomes ours: its import posting, which carried QBO's
    cost of goods, has just been reversed."""
    from app.services.accounting import _q, create_journal_entry
    from app.services.inventory_service import (
        get_cogs_account_id,
        get_inventory_asset_account_id,
    )

    if local_cost_live(db, invoice):
        return
    for movement in db.query(InventoryMovement).filter(
        InventoryMovement.source_type == "invoice",
        InventoryMovement.source_id == invoice.id,
        InventoryMovement.movement_type == MovementType.SALE,
    ):
        item = db.get(Item, movement.item_id)
        amount = _q(
            abs(Decimal(str(movement.quantity))) * Decimal(str(movement.unit_cost or 0))
        )
        asset_id = get_inventory_asset_account_id(db, item) if item else None
        cogs_id = get_cogs_account_id(db)
        if amount <= 0 or not (asset_id and cogs_id):
            continue
        txn = create_journal_entry(
            db,
            invoice.date,
            f"COGS — {item.name}",
            [
                {
                    "account_id": cogs_id,
                    "debit": amount,
                    "credit": Decimal("0"),
                    "description": f"COGS: {item.name}",
                },
                {
                    "account_id": asset_id,
                    "debit": Decimal("0"),
                    "credit": amount,
                    "description": f"Inventory: {item.name}",
                },
            ],
            source_type="invoice",
            source_id=invoice.id,
        )
        movement.transaction_id = txn.id


# ---------------------------------------------------------------------------
# Voids
# ---------------------------------------------------------------------------


def void_invoice_import_posting(db: Session, invoice, what: str) -> bool:
    """For an invoice or sales receipt the QBO import created, reverse the
    posting the ledger import made for it and mark it changed here. Returns
    whether its stock was costed by that posting (so the stock goes back
    without a cost-of-goods entry of ours). A no-op for any other invoice."""
    mapping = invoice_origin(db, invoice)
    if mapping is None:
        return False
    carried = not local_cost_live(db, invoice)
    found = import_posting(db, mapping)
    ledger = None
    if found:
        ledger, txn = found
        reverse_import_posting(
            db,
            txn,
            refusal=(
                f"This {what}'s posting is on a reconciled bank statement, so it "
                "can't be voided."
            ),
        )
    mark_changed_here(db, mapping, ledger)
    return carried


def void_payment_import_posting(db: Session, payment) -> None:
    """For a payment the QBO import created, reverse the posting the ledger
    import made for it: the payment's own, or, for a sales receipt's
    payment, the receipt's (which carries the cash and the income; the
    receipt is voided next, and posts nothing of its own either)."""
    mapping = payment_origin(db, payment)
    if mapping is None:
        return
    found = import_posting(db, mapping)
    ledger = None
    if found:
        ledger, txn = found
        reverse_import_posting(
            db,
            txn,
            refusal=(
                "This payment is on a bank statement that has been reconciled, "
                "so it can't be voided. If the check bounced, charge the "
                "customer again with a new invoice."
            ),
        )
    mark_changed_here(db, mapping, ledger)


def money_in_posting(db: Session, payment):
    """Where a payment's money came in: its own posting, or the import
    posting of a payment the QBO import created (for the deposit check)."""
    if payment.transaction_id is not None:
        return None
    found = import_posting(db, payment_origin(db, payment))
    return found[1] if found else None


def document_of_posting(db: Session, txn):
    """(kind, document) when `txn` is the ledger import's posting of an
    invoice, sales receipt or payment the QBO import created and that still
    stands; None for any other posting (a journal, a purchase, a deposit)."""
    kinds = {kind: qbo_type for kind, qbo_type in LEDGER_TYPE.items()}
    for ledger in db.query(QBOMapping).filter(
        QBOMapping.entity_type == "ledger", QBOMapping.slowbooks_id == txn.id
    ):
        qbo_type, _, qbo_id = ledger.qbo_id.partition(":")
        wanted = qbo_type.lower().replace(" ", "")
        for kind, name in kinds.items():
            if name.lower().replace(" ", "") != wanted:
                continue
            mapping = (
                db.query(QBOMapping)
                .filter(QBOMapping.entity_type == kind, QBOMapping.qbo_id == qbo_id)
                .first()
            )
            if mapping is None:
                continue
            if kind == "payment":
                document = db.get(Payment, mapping.slowbooks_id)
                if document is not None and not document.is_voided:
                    return kind, document
            else:
                document = db.get(Invoice, mapping.slowbooks_id)
                if document is not None and document.status != InvoiceStatus.VOID:
                    return kind, document
    return None


# ---------------------------------------------------------------------------
# Edits: the document becomes ours
# ---------------------------------------------------------------------------


def post_payment_journal(db: Session, payment) -> None:
    """A payment's own posting (DR the account it went to / CR A/R), for the
    payment of a QBO sales receipt that becomes ours."""
    from app.models.contacts import Customer
    from app.services.accounting import (
        create_journal_entry,
        get_ar_account_id,
        get_undeposited_funds_id,
    )

    customer = db.get(Customer, payment.customer_id)
    amount = Decimal(str(payment.amount))
    txn = create_journal_entry(
        db,
        payment.date,
        f"Payment from {customer.name if customer else 'Unknown'}",
        [
            {
                "account_id": payment.deposit_to_account_id
                or get_undeposited_funds_id(db),
                "debit": amount,
                "credit": Decimal("0"),
                "description": "Payment received",
            },
            {
                "account_id": get_ar_account_id(db),
                "debit": Decimal("0"),
                "credit": amount,
                "description": "Payment received",
            },
        ],
        source_type="payment",
        source_id=payment.id,
        reference=payment.reference or "",
    )
    payment.transaction_id = txn.id


def adopt_invoice(db: Session, invoice) -> bool:
    """Make an invoice or sales receipt the QBO import created an ordinary
    one of ours, before an edit changes its amounts or date. Its import
    posting is reversed (the stock it moved is costed here again), a sales
    receipt's payment gets its posting, and the caller posts the invoice's
    own entry from its edited lines. Returns False for any other invoice."""
    mapping = invoice_origin(db, invoice)
    if mapping is None:
        return False
    what = "sales receipt" if mapping.entity_type == "sales_receipt" else "invoice"
    found = import_posting(db, mapping)
    ledger = None
    if found:
        ledger, txn = found
        reverse_import_posting(
            db,
            txn,
            refusal=(
                f"This {what}'s posting is on a reconciled bank statement, so its "
                "amounts and date can't be changed."
            ),
        )
    # Its stock is costed here from now on (a no-op while a cost of ours
    # stands: the ledger import never posted it).
    restore_local_cost(db, invoice)
    if mapping.entity_type == "sales_receipt":
        for alloc in invoice.payment_allocations:
            payment = alloc.payment
            if payment and not payment.is_voided and payment.transaction_id is None:
                post_payment_journal(db, payment)
    mark_changed_here(db, mapping, ledger)
    adopt_unposted_payments(db, invoice)
    return True


def adopt_unposted_payments(db: Session, invoice) -> None:
    """The QBO payments on an invoice that becomes ours, when they have no
    posting at all (the ledger import never posted them): ours too, and
    posted, so A/R is right at once (the invoice posts its own debit). A
    later ledger import keeps them (CHANGED_HERE).

    Such a payment may also pay other invoices the import made, with no
    posting either; those become ours with it, so every part of its credit
    to A/R meets an invoice's debit there. With the ledger import run, each
    payment already has its posting, and nothing here changes."""
    from app.routes.invoices.helpers import _post_invoice_journal

    invoices, payments = [invoice], set()
    seen = {invoice.id}
    while invoices:
        paid = invoices.pop()
        for alloc in list(paid.payment_allocations):
            payment = alloc.payment
            if payment is None or payment.id in payments or payment.is_voided:
                continue
            payments.add(payment.id)
            mapping = origin(db, ("payment",), payment.id)
            if (
                mapping is None
                or payment.transaction_id is not None
                or import_posting(db, mapping) is not None
            ):
                continue
            post_payment_journal(db, payment)
            mark_changed_here(db, mapping)
            for other in payment.allocations:
                sibling = other.invoice
                if sibling is None or sibling.id in seen:
                    continue
                seen.add(sibling.id)
                sibling_map = invoice_origin(db, sibling)
                if (
                    sibling_map is None
                    or sibling.status == InvoiceStatus.VOID
                    or import_posting(db, sibling_map) is not None
                ):
                    continue
                restore_local_cost(db, sibling)
                txn = _post_invoice_journal(
                    db,
                    sibling,
                    list(sibling.lines),
                    sibling.customer.name if sibling.customer else "",
                    balance_on_income=True,
                )
                sibling.transaction_id = txn.id
                mark_changed_here(db, sibling_map)
                invoices.append(sibling)


# ---------------------------------------------------------------------------
# A repair for what the import left wrong (#317)
# ---------------------------------------------------------------------------


def repair_double_counted_payments(db: Session) -> int:
    """#317: the connector's invoice import set an invoice's amount paid from
    QuickBooks Online (its total less its balance), and its payment import
    then added each payment's allocation to it again. A paid invoice read
    paid twice (a 11,666.66 invoice paid 23,333.32, due -11,666.66), a
    partly paid one owed too little, and the aging, statements and open
    lists were off; the ledger was right.

    The repair is exact, not a recompute of every invoice: an invoice the
    import created whose amount paid, less any credit memos applied here,
    is twice what the payments applied to it is set to what the payments
    and credits applied, with its balance and status. Any other invoice
    (one whose QuickBooks Online payments did not all come across, one
    never imported) is left alone. Runs at startup; a second pass finds
    nothing. Returns the number repaired."""
    from sqlalchemy import func

    from app.models.credit_memos import CreditApplication, CreditMemo, CreditMemoStatus
    from app.models.payments import PaymentAllocation

    imported = [
        sid
        for (sid,) in db.query(QBOMapping.slowbooks_id).filter(
            QBOMapping.entity_type.in_(("invoice", "sales_receipt"))
        )
    ]
    if not imported:
        return 0
    paid = dict(
        db.query(PaymentAllocation.invoice_id, func.sum(PaymentAllocation.amount))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(PaymentAllocation.invoice_id.in_(imported))
        .filter(Payment.is_voided.isnot(True))
        .group_by(PaymentAllocation.invoice_id)
    )
    credited = dict(
        db.query(CreditApplication.invoice_id, func.sum(CreditApplication.amount))
        .join(CreditMemo, CreditMemo.id == CreditApplication.credit_memo_id)
        .filter(CreditApplication.invoice_id.in_(imported))
        .filter(CreditMemo.status != CreditMemoStatus.VOID)
        .group_by(CreditApplication.invoice_id)
    )
    repaired = 0
    for invoice in db.query(Invoice).filter(Invoice.id.in_(imported)):
        if invoice.status == InvoiceStatus.VOID:
            continue
        applied = Decimal(str(paid.get(invoice.id) or 0))
        credits = Decimal(str(credited.get(invoice.id) or 0))
        if applied <= 0:
            continue
        if Decimal(str(invoice.amount_paid or 0)) - credits != applied * 2:
            continue
        invoice.amount_paid = applied + credits
        invoice.balance_due = Decimal(str(invoice.total or 0)) - invoice.amount_paid
        invoice.status = (
            InvoiceStatus.PAID if invoice.balance_due <= 0 else InvoiceStatus.PARTIAL
        )
        repaired += 1
    if repaired:
        db.commit()
    return repaired
