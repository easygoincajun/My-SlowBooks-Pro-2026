"""a PTO request can be cancelled (issue #262)

An approved time-off request could only stay approved: when the trip
fell through, the hours stayed out of the employee's bank and the relief
journal stayed posted. From here a request has a fourth state, cancelled,
and the approval records what it took out of the bank — the hours that
were really there (an approval can overdraw a bank to zero) and the
dollars the relief journal moved — so a cancel puts back exactly that.

Two nullable columns on pto_requests. NULL on a request approved before
this build; a cancel of one of those assumes the request's own hours.

PostgreSQL keeps the status in a native enum type (ptorequeststatus), so
the new value is added to it. SQLite stores the name as text with no
constraint, so nothing there changes. A downgrade drops the two columns
and turns cancelled requests into denied ones — the older build has no
word for cancelled, and PostgreSQL cannot drop a value from an enum type.

Revision ID: b4d6f8a0c2e4
Revises: e2b7c4d9a1f3
Create Date: 2026-10-11

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b4d6f8a0c2e4"
down_revision: Union[str, None] = "e2b7c4d9a1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ADD VALUE wants to run outside the migration's transaction
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE ptorequeststatus ADD VALUE IF NOT EXISTS 'CANCELLED'"
            )
    with op.batch_alter_table("pto_requests") as batch:
        batch.add_column(sa.Column("hours_drawn", sa.Numeric(10, 2), nullable=True))
        batch.add_column(
            sa.Column("dollars_relieved", sa.Numeric(12, 2), nullable=True)
        )


def downgrade() -> None:
    op.execute("UPDATE pto_requests SET status = 'DENIED' WHERE status = 'CANCELLED'")
    with op.batch_alter_table("pto_requests") as batch:
        batch.drop_column("dollars_relieved")
        batch.drop_column("hours_drawn")
