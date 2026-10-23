# ============================================================================
# Accountant's copies (#346): the copies of these books sent out to an
# accountant, so their changes can be taken back once and only from a copy
# these books made.
# ============================================================================

from sqlalchemy import JSON, Column, Date, DateTime, String, func

from app.database import Base


class AccountantsCopy(Base):
    __tablename__ = "accountants_copies"

    # the copy's identity, carried in the copy and in every change file
    copy_id = Column(String(36), primary_key=True)
    # the secret the change file is signed with; the copy carries it too
    token = Column(String(64), nullable=False)
    company_name = Column(String(200), nullable=False, default="")
    # the accountant works on or before this day; the business after it
    dividing_date = Column(Date, nullable=False)
    made_at = Column(DateTime(timezone=True), server_default=func.now())
    # the change-file entry ids already posted here, so a file applied
    # twice posts nothing twice
    applied = Column(JSON, nullable=False, default=list)
