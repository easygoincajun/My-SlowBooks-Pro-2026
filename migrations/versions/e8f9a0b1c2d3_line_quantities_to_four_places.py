"""line quantities to four decimal places

The quantity on invoice, estimate, credit memo, recurring invoice, bill,
purchase order and vendor credit lines was Numeric(10, 2), so a quantity
finer than the hundredth could not be kept: the IIF import and the
QuickBooks report import stored the source's 2.125 and every read gave
2.12 (SQLite) or 2.13 (PostgreSQL), the edit form showed the rounded
quantity with a total 40 cents off, and the IIF export wrote QNTY -2.12
beside the line's real amount (2.23.0 gate, NEW-94). Numeric(12, 4) keeps
the same eight integer digits and adds two decimal places, so every stored
value fits and none changes; unit prices took the same step in
6f57f762f464. Line amounts stay to the cent. PostgreSQL alters the column
in place; SQLite rewrites the table (batch_alter_table).

The downgrade rounds quantities back to the hundredth.

Revision ID: e8f9a0b1c2d3
Revises: d6f7a8b9c0e1
Create Date: 2026-10-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, None] = "d6f7a8b9c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("invoice_lines", "quantity"),
    ("estimate_lines", "quantity"),
    ("credit_memo_lines", "quantity"),
    ("recurring_invoice_lines", "quantity"),
    ("bill_lines", "quantity"),
    ("purchase_order_lines", "quantity"),
    ("purchase_order_lines", "received_qty"),
    ("vendor_credit_lines", "quantity"),
)


def _set(new: sa.Numeric, old: sa.Numeric) -> None:
    insp = sa.inspect(op.get_bind())
    present = set(insp.get_table_names())
    for table, column in COLUMNS:
        if table not in present:
            continue
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=old, type_=new)


def upgrade() -> None:
    _set(sa.Numeric(12, 4), sa.Numeric(10, 2))


def downgrade() -> None:
    _set(sa.Numeric(10, 2), sa.Numeric(12, 4))
