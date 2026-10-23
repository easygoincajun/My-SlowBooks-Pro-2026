# ============================================================================
# Users — the principal model (Server Edition).
#
# One row per person who can sign in. Single-operator installs have
# exactly one admin row (backfilled from the legacy settings-table
# password hash on first login after upgrade) and never see a username
# field. Roles are stored now, enforced by the RBAC layer.
# ============================================================================

from datetime import datetime, timezone

from sqlalchemy import Date, Boolean, Column, DateTime, Integer, String

from sqlalchemy import false as sa_false
from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


ROLE_ADMIN = "admin"
ROLE_BOOKKEEPER = "bookkeeper"
ROLE_READONLY = "readonly"
# An outside accountant's sign-in (#340): reads everything, the audit log
# included, and writes journal entries only (posts them, voids its own).
ROLE_ACCOUNTANT = "accountant"
VALID_ROLES = (ROLE_ADMIN, ROLE_BOOKKEEPER, ROLE_ACCOUNTANT, ROLE_READONLY)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # Stored lowercase; lookups lowercase their input.
    username = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False, default="")
    password_hash = Column(String(512), nullable=False)
    role = Column(String(20), nullable=False, default=ROLE_ADMIN)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)
    # An invited sign-in (#345): the one-time password must be replaced at
    # the first sign-in, and the sign-in can end on a day.
    must_change_password = Column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )
    expires_on = Column(Date, nullable=True)
    # Revocable sessions: a sign-in records this number; every request
    # re-reads the row and signs the session out when the two differ. Bumped
    # on deactivation, a role change, and a password change/reset, so those
    # end the user's existing sessions at once instead of waiting for the
    # idle timeout — the signed cookie is no longer trusted on its own.
    session_version = Column(Integer, nullable=False, default=0, server_default="0")
