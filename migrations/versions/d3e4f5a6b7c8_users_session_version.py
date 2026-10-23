"""revocable sessions: users.session_version

The auth middleware took the role straight from the signed
session cookie and never re-read the user, so deactivating a user, changing
their role, or resetting their password left their existing sessions alive
until the idle timeout. A per-user version number, recorded in the session
and re-read on every request, makes those sessions end on the next request:
the number is bumped on the row, the stored one no longer matches, the
session is signed out.

One number per user row, so it lives in the users table of each company
file (the multi-company server of #358 serves one such table per company;
there is no separate master database). Existing rows take 0 — nobody is
signed out by the upgrade itself; a session from before this column simply
adopts the row's current number the first time it is re-checked.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUMN = sa.Column(
    "session_version",
    sa.Integer(),
    nullable=False,
    server_default="0",
)


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "users" not in insp.get_table_names():
        return
    present = {c["name"] for c in insp.get_columns("users")}
    if COLUMN.name not in present:
        op.add_column("users", COLUMN.copy())


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "users" not in insp.get_table_names():
        return
    present = {c["name"] for c in insp.get_columns("users")}
    if COLUMN.name in present:
        with op.batch_alter_table("users") as batch:
            batch.drop_column(COLUMN.name)
