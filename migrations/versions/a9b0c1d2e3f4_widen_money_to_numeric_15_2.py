"""widen money columns from Numeric(12, 2) to Numeric(15, 2)

Numeric(12, 2) holds at most 9,999,999,999.99. Precision 15 keeps two
decimal places and raises the cap to 9,999,999,999,999.99.

Columns are discovered from the live schema so both spellings already in
the history (Numeric(12, 2) and Numeric(precision=12, scale=2)) are
widened. PostgreSQL reads information_schema. SQLite (desktop company
files and the test suite) has no information_schema, so the same columns
are reflected instead. batch_alter_table rewrites them there, because
SQLite cannot ALTER COLUMN TYPE; on PostgreSQL the type change is an
ALTER.

Other precisions are left alone: Numeric(10, 2) quantities, Numeric(12, 4)
rates, Numeric(14, 4) inventory, Numeric(5, 4) tax rates, Numeric(18, 8)
exchange rates.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-09-23

"""

from collections import defaultdict
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _money_columns(precision: int) -> list[tuple[str, str]]:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        rows = bind.execute(
            sa.text("""
                SELECT c.table_name, c.column_name
                FROM information_schema.columns c
                JOIN information_schema.tables t
                  ON t.table_schema = c.table_schema
                 AND t.table_name = c.table_name
                WHERE c.table_schema = current_schema()
                  AND t.table_type = 'BASE TABLE'
                  AND c.data_type = 'numeric'
                  AND c.numeric_precision = :precision
                  AND c.numeric_scale = 2
                ORDER BY c.table_name, c.column_name
                """),
            {"precision": precision},
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    insp = sa.inspect(bind)
    found = []
    for table in insp.get_table_names():
        for col in insp.get_columns(table):
            typ = col["type"]
            if (
                getattr(typ, "precision", None) == precision
                and getattr(typ, "scale", None) == 2
            ):
                found.append((table, col["name"]))
    found.sort()
    return found


def _set_precision(
    columns: list[tuple[str, str]], new: sa.Numeric, old: sa.Numeric
) -> None:
    by_table: dict[str, list[str]] = defaultdict(list)
    for table, column in columns:
        by_table[table].append(column)
    for table in sorted(by_table):
        with op.batch_alter_table(table) as batch:
            for column in by_table[table]:
                batch.alter_column(column, existing_type=old, type_=new)


def upgrade() -> None:
    _set_precision(_money_columns(12), sa.Numeric(15, 2), sa.Numeric(12, 2))


def downgrade() -> None:
    _set_precision(_money_columns(15), sa.Numeric(12, 2), sa.Numeric(15, 2))
