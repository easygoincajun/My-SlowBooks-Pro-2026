"""employees' portal links kept as a digest and an encrypted copy

employees.portal_token held each employee's self-service link as issued.
Anyone with a copy of the company file, or a backup, had every employee's
working link, and a link signs in as that employee, the bank account their
pay goes to included. From here the portal looks a link up by its SHA-256
(portal_token_hash), and the copy an administrator can show again is
encrypted with the payroll key (portal_token_enc), which is kept outside
the database. Links already sent keep working: each is moved over here and
the column that held it is emptied.

The encryption is the payroll fields' (app/services/encryption.py: Fernet,
PBKDF2-SHA256 of PAYROLL_ENCRYPTION_SECRET, "v1:" prefix), written out here
so this migration never changes with that module. The environment (and the
install's .env, which env.py loads through app.config) has the secret when
the launcher, Docker's entrypoint or a restore migrates. If the copy can't
be made, only the digest is kept: the link still works, and the portal keeps
a copy again the next time the employee uses it.

Revision ID: e2b7c4d9a1f3
Revises: c5e1f7a9b3d2
Create Date: 2026-09-27

"""

import base64
import hashlib
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e2b7c4d9a1f3"
down_revision: Union[str, None] = "c5e1f7a9b3d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PAYROLL_PLACEHOLDER = "slowbooks-dev-payroll-key-change-me"
_VERSION_PREFIX = "v1:"

_employees = sa.table(
    "employees",
    sa.column("id", sa.Integer),
    sa.column("portal_token", sa.String),
    sa.column("portal_token_hash", sa.String),
    sa.column("portal_token_enc", sa.Text),
)


def _payroll_fernet():
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        # exactly as app/config.py reads it, so the key is the app's
        secret = os.getenv("PAYROLL_ENCRYPTION_SECRET", _PAYROLL_PLACEHOLDER)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"slowbooks-payroll-v1",
            iterations=480000,
        )
        return Fernet(base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8"))))
    except Exception:
        return None


def upgrade() -> None:
    op.add_column(
        "employees", sa.Column("portal_token_hash", sa.String(64), nullable=True)
    )
    op.add_column("employees", sa.Column("portal_token_enc", sa.Text(), nullable=True))
    op.create_index(
        "ix_employees_portal_token_hash",
        "employees",
        ["portal_token_hash"],
        unique=True,
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_employees.c.id, _employees.c.portal_token).where(
            _employees.c.portal_token.isnot(None)
        )
    ).fetchall()
    fernet = _payroll_fernet() if rows else None
    for row in rows:
        token = row.portal_token
        enc = None
        if fernet is not None:
            try:
                enc = _VERSION_PREFIX + fernet.encrypt(token.encode("utf-8")).decode(
                    "ascii"
                )
            except Exception:
                enc = None
        bind.execute(
            _employees.update()
            .where(_employees.c.id == row.id)
            .values(
                portal_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                portal_token_enc=enc,
                portal_token=None,
            )
        )


def downgrade() -> None:
    # Links whose copy opens go back as issued; one that doesn't can't be
    # recovered from its digest, and that employee needs a new link.
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_employees.c.id, _employees.c.portal_token_enc).where(
            _employees.c.portal_token_enc.isnot(None)
        )
    ).fetchall()
    fernet = _payroll_fernet() if rows else None
    for row in rows:
        token = None
        if fernet is not None:
            try:
                raw = row.portal_token_enc
                if raw.startswith(_VERSION_PREFIX):
                    raw = raw[len(_VERSION_PREFIX) :]
                token = fernet.decrypt(raw.encode("ascii")).decode("utf-8")
            except Exception:
                token = None
        bind.execute(
            _employees.update()
            .where(_employees.c.id == row.id)
            .values(portal_token=token)
        )
    op.drop_index("ix_employees_portal_token_hash", table_name="employees")
    op.drop_column("employees", "portal_token_enc")
    op.drop_column("employees", "portal_token_hash")
