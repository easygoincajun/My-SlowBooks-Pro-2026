# ============================================================================
# Retry-safe request identity for the payment-recording routes (#292).
#
# A POST that commits and then loses its response is retried by a client
# and books the payment twice: the over-allocation guard cannot tell a
# replay from a second payment while balance remains. A caller that sends
# an Idempotency-Key header gets one row here per route and key: reserved
# before the write, filled with the response after it, in the same
# transaction as the payment. The same key with the same payload returns
# that response; the same key with another payload is refused.
# ============================================================================

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, func

from app.database import Base


class RequestKey(Base):
    __tablename__ = "request_keys"
    __table_args__ = (
        UniqueConstraint("route", "key", name="uq_request_keys_route_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    route = Column(String(80), nullable=False)  # "POST /api/payments"
    key = Column(String(128), nullable=False)
    payload_hash = Column(String(64), nullable=False)  # sha256 of the body, canonical
    status_code = Column(Integer, nullable=True)  # NULL while the first delivery runs
    response_json = Column(Text, nullable=True)
    actor = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
