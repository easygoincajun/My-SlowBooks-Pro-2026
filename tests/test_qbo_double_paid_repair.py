"""Invoices the QuickBooks Online import counted as paid twice are repaired
(2.23.3, #317).

The retired connector's invoice import set amount_paid from QuickBooks
Online (its total less its balance); its payment import then added each
payment's allocation again. The ledger was right; the invoices read paid
twice, or owed too little when partly paid. The startup repair fixes the
exact signature on invoices the import created and leaves every other
invoice alone.
"""

from datetime import date
from decimal import Decimal

from app.models.contacts import Customer
from app.models.credit_memos import CreditApplication, CreditMemo, CreditMemoStatus
from app.models.invoices import Invoice, InvoiceStatus
from app.models.payments import Payment, PaymentAllocation
from app.services.qbo_common import create_mapping
from app.services.qbo_documents import repair_double_counted_payments

DAY = date(2026, 8, 3)


def _invoice(db, customer, number, total, paid, *, imported=True):
    inv = Invoice(
        invoice_number=number,
        customer_id=customer.id,
        date=DAY,
        due_date=DAY,
        total=Decimal(total),
        subtotal=Decimal(total),
        amount_paid=Decimal(paid),
        balance_due=Decimal(total) - Decimal(paid),
        status=InvoiceStatus.PARTIAL,
    )
    db.add(inv)
    db.flush()
    if imported:
        create_mapping(db, "invoice", inv.id, f"Q{number}")
    return inv


def _pay(db, customer, invoice, amount, *, voided=False):
    payment = Payment(
        customer_id=customer.id, date=DAY, amount=Decimal(amount), is_voided=voided
    )
    db.add(payment)
    db.flush()
    db.add(
        PaymentAllocation(
            payment_id=payment.id, invoice_id=invoice.id, amount=Decimal(amount)
        )
    )
    db.flush()


def test_the_double_count_is_repaired_and_nothing_else_moves(db_session):
    db = db_session
    who = Customer(name="Harbor Light")
    db.add(who)
    db.flush()
    # paid in full in QuickBooks Online, counted twice here
    full = _invoice(db, who, "1001", "11666.66", "23333.32")
    _pay(db, who, full, "11666.66")
    # partly paid (400 of 1,000), counted twice: owed 200 instead of 600
    part = _invoice(db, who, "1002", "1000", "800")
    _pay(db, who, part, "400")
    # paid twice over with a credit memo applied here afterwards
    credit = _invoice(db, who, "1003", "500", "500")  # 200 + 200 doubled + 100 credit
    _pay(db, who, credit, "200")
    memo = CreditMemo(
        memo_number="CM-1",
        customer_id=who.id,
        date=DAY,
        total=Decimal("100"),
        amount_applied=Decimal("100"),
        balance_remaining=Decimal("0"),
        status=CreditMemoStatus.APPLIED,
    )
    db.add(memo)
    db.flush()
    db.add(CreditApplication(credit_memo_id=memo.id, invoice_id=credit.id, amount=100))
    # right already: one payment, counted once
    right = _invoice(db, who, "1004", "300", "300")
    _pay(db, who, right, "300")
    # a payment of it did not come across: not the signature, left alone
    short = _invoice(db, who, "1005", "1000", "1500")
    _pay(db, who, short, "500")
    # not imported: never touched, whatever it reads
    native = _invoice(db, who, "2001", "100", "200", imported=False)
    _pay(db, who, native, "100")
    # a voided payment does not count as applied
    void = _invoice(db, who, "1006", "100", "100")
    _pay(db, who, void, "100", voided=True)
    db.commit()

    assert repair_double_counted_payments(db) == 3
    for inv in (full, part, credit, right, short, native, void):
        db.refresh(inv)
    assert (full.amount_paid, full.balance_due, full.status) == (
        Decimal("11666.66"),
        Decimal("0"),
        InvoiceStatus.PAID,
    )
    assert (part.amount_paid, part.balance_due, part.status) == (
        Decimal("400"),
        Decimal("600"),
        InvoiceStatus.PARTIAL,
    )
    assert (credit.amount_paid, credit.balance_due) == (Decimal("300"), Decimal("200"))
    assert right.amount_paid == Decimal("300")
    assert short.amount_paid == Decimal("1500")
    assert native.amount_paid == Decimal("200")
    assert void.amount_paid == Decimal("100")
    # a second pass finds nothing
    assert repair_double_counted_payments(db) == 0
