"""retry-safe request identity for payment recording (issue #292)

One row per route and Idempotency-Key: reserved before the payment is
written, filled with the response after it, in the same transaction, so a
retry of a request whose response was lost returns that response instead
of booking the payment again.

Revision ID: d6f7a8b9c0e1
Revises: c5e7a9b1d3f5
Create Date: 2026-10-13

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d6f7a8b9c0e1"
down_revision: Union[str, None] = "c5e7a9b1d3f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "request_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("route", sa.String(length=80), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("route", "key", name="uq_request_keys_route_key"),
    )
    op.create_index("ix_request_keys_id", "request_keys", ["id"])


def downgrade() -> None:
    op.drop_index("ix_request_keys_id", table_name="request_keys")
    op.drop_table("request_keys")
