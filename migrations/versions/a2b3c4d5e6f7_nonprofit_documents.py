"""nonprofit_documents

Nonprofit mode, part two: the documents. A release from restriction
(DR net assets with donor restrictions / CR without, tagged to the fund),
saved allocation rules with their targets, and the period-end functional
allocation that re-runs a rule on a shared account. All new tables; no
existing row changes.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-05

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps():
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )


def upgrade() -> None:
    op.create_table(
        "restriction_releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("number", sa.String(length=30), nullable=False, unique=True),
        sa.Column("date", sa.Date(), nullable=False, index=True),
        sa.Column(
            "class_id",
            sa.Integer(),
            sa.ForeignKey("classes.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
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
        *_timestamps(),
    )
    op.create_table(
        "allocation_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column(
            "basis", sa.String(length=20), nullable=False, server_default="percent"
        ),
        sa.Column(
            "source_account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id"),
            nullable=True,
        ),
        sa.Column(
            "source_class_id", sa.Integer(), sa.ForeignKey("classes.id"), nullable=True
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_table(
        "allocation_rule_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "rule_id",
            sa.Integer(),
            sa.ForeignKey("allocation_rules.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("class_id", sa.Integer(), sa.ForeignKey("classes.id"), nullable=True),
        sa.Column("function", sa.String(length=20), nullable=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("weight", sa.Numeric(12, 4), nullable=False, server_default="1"),
        sa.Column("line_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "functional_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("number", sa.String(length=30), nullable=False, unique=True),
        sa.Column("date", sa.Date(), nullable=False, index=True),
        sa.Column(
            "rule_id", sa.Integer(), sa.ForeignKey("allocation_rules.id"), nullable=True
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
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
        *_timestamps(),
    )
    op.create_table(
        "functional_allocation_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "allocation_id",
            sa.Integer(),
            sa.ForeignKey("functional_allocations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("class_id", sa.Integer(), sa.ForeignKey("classes.id"), nullable=True),
        sa.Column("function", sa.String(length=20), nullable=True),
        sa.Column("weight", sa.Numeric(12, 4), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("line_order", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("functional_allocation_lines")
    op.drop_table("functional_allocations")
    op.drop_table("allocation_rule_targets")
    op.drop_table("allocation_rules")
    op.drop_table("restriction_releases")
