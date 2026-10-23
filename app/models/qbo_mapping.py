# ============================================================================
# QBO Entity Mapping — which local record a QuickBooks Online record became.
#
# Written by the QuickBooks Online connector's import while it existed (the
# connector was retired in 2.23.2; services/qbo_common.py says why). The
# table stays so that a company connected back then still knows which of
# its documents the import created (services/qbo_documents.py) and which
# item a QuickBooks Online discount came in on. Nothing writes to it now.
# ============================================================================

from sqlalchemy import Column, Integer, String, DateTime, func

from app.database import Base


class QBOMapping(Base):
    __tablename__ = "qbo_mappings"

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(
        String(50), nullable=False
    )  # account, customer, vendor, item, invoice, payment
    slowbooks_id = Column(Integer, nullable=False)
    qbo_id = Column(String(100), nullable=False)
    qbo_sync_token = Column(String(50), nullable=True)
    last_synced_at = Column(DateTime(timezone=True), server_default=func.now())
