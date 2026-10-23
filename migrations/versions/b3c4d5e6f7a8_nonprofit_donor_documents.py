"""nonprofit_donor_documents

Nonprofit mode, part three: what a donor is handed. An invoice can be a
pledge (printed as one) and a donation receipt can state the fair value
of what the donor got back (the gala dinner) so the deductible portion
prints per IRS Publication 1771. Generated invoices now remember the
recurring template they came from — the pledge report needs it, and a
best-effort backfill links existing ones where the customer has a single
template. A credit memo can be a write-off (bad debt). A customer gains
the three donor fields the acknowledgment letter and the year-end
giving statement use. In-kind gifts get their own two-sided document.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-09-06

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.add_column(
            sa.Column("fair_value_amount", sa.Numeric(12, 2), nullable=True)
        )
        batch_op.add_column(
            sa.Column("fair_value_description", sa.String(length=200), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "is_pledge", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column("recurring_invoice_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_invoices_recurring_invoice_id",
            "recurring_invoices",
            ["recurring_invoice_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_invoices_recurring_invoice_id", ["recurring_invoice_id"]
        )
    with op.batch_alter_table("credit_memos") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_write_off", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(
            sa.Column("donor_type", sa.String(length=20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("salutation", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "send_year_end_statement",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )

    op.create_table(
        "in_kind_gifts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("number", sa.String(length=30), nullable=False, unique=True),
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customers.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("date", sa.Date(), nullable=False, index=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=10), nullable=False, server_default="posted"
        ),
        sa.Column(
            "transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=True,
        ),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("class_id", sa.Integer(), sa.ForeignKey("classes.id"), nullable=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_table(
        "in_kind_gift_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "gift_id",
            sa.Integer(),
            sa.ForeignKey("in_kind_gifts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False, server_default="1"),
        sa.Column("fair_value", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column(
            "debit_account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "credit_account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column("class_id", sa.Integer(), sa.ForeignKey("classes.id"), nullable=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("line_order", sa.Integer(), nullable=False, server_default="0"),
    )

    # Backfill: an invoice whose posting says "Recurring Invoice #..." and
    # whose customer has exactly one template came from that template.
    op.execute(sa.text("""
            UPDATE invoices SET recurring_invoice_id = (
                SELECT r.id FROM recurring_invoices r
                WHERE r.customer_id = invoices.customer_id
            )
            WHERE recurring_invoice_id IS NULL
              AND (SELECT COUNT(*) FROM recurring_invoices r2
                   WHERE r2.customer_id = invoices.customer_id) = 1
              AND EXISTS (
                SELECT 1 FROM transactions t
                WHERE t.source_type = 'invoice' AND t.source_id = invoices.id
                  AND t.description LIKE 'Recurring Invoice #%'
              )
            """))


def downgrade() -> None:
    op.drop_table("in_kind_gift_lines")
    op.drop_table("in_kind_gifts")
    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_column("send_year_end_statement")
        batch_op.drop_column("salutation")
        batch_op.drop_column("donor_type")
    with op.batch_alter_table("credit_memos") as batch_op:
        batch_op.drop_column("is_write_off")
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.drop_index("ix_invoices_recurring_invoice_id")
        batch_op.drop_constraint("fk_invoices_recurring_invoice_id", type_="foreignkey")
        batch_op.drop_column("recurring_invoice_id")
        batch_op.drop_column("is_pledge")
        batch_op.drop_column("fair_value_description")
        batch_op.drop_column("fair_value_amount")
