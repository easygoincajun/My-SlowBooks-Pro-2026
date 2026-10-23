"""who generated a tax form (issue #263)

document_audits kept what was generated and when; the Form history page
under Tax Forms also says who. The signed-in user's name — the same actor
the audit log records — goes on the row as the form is generated. NULL on
rows from before this build: the page shows those without a name.

Revision ID: c5e7a9b1d3f5
Revises: b4d6f8a0c2e4
Create Date: 2026-10-11

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c5e7a9b1d3f5"
down_revision: Union[str, None] = "b4d6f8a0c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("document_audits") as batch:
        batch.add_column(sa.Column("username", sa.String(100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("document_audits") as batch:
        batch.drop_column("username")
