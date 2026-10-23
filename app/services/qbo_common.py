# ============================================================================
# What an earlier QuickBooks Online import left behind.
#
# The QuickBooks Online connector (Intuit OAuth, import and export over the
# API) was retired in 2.23.2: keys that reach a real company sit behind
# Intuit's app questionnaire, so it never served an end user. QuickBooks
# Online is migrated from its own Export data zip now
# (services/qbo_export_import.py).
#
# A company that was connected while the connector existed still carries the
# mappings it wrote (qbo_mappings): which local document, account or item a
# QuickBooks Online record became. The documents those mappings name have no
# posting of their own, so voiding or editing one still reverses the posting
# the import made for it (services/qbo_documents.py), and an item a discount
# came in on still takes a negative line. Nothing writes a new mapping any
# more, apart from the tests.
# ============================================================================

from sqlalchemy.orm import Session

from app.models.qbo_mapping import QBOMapping


def get_mapping_by_qbo_id(db: Session, entity_type: str, qbo_id: str) -> QBOMapping:
    """Look up an existing mapping by its QuickBooks Online id."""
    return (
        db.query(QBOMapping)
        .filter(
            QBOMapping.entity_type == entity_type,
            QBOMapping.qbo_id == str(qbo_id),
        )
        .first()
    )


def create_mapping(
    db: Session,
    entity_type: str,
    slowbooks_id: int,
    qbo_id: str,
    sync_token: str = None,
):
    """A mapping as the import wrote them (the tests' way to make a document
    the import created)."""
    db.add(
        QBOMapping(
            entity_type=entity_type,
            slowbooks_id=slowbooks_id,
            qbo_id=str(qbo_id),
            qbo_sync_token=sync_token,
        )
    )


# A mapping's sync token once the document was changed here: the import
# left such a document alone instead of posting it again.
CHANGED_HERE = "changed-in-slowbooks"  # voided or replaced here: kept as is
KEPT_HERE = "kept-in-slowbooks"  # the same, once an import's log said so
HERE = (CHANGED_HERE, KEPT_HERE)


# The item a QuickBooks Online discount came in on: a mapping of this entity
# type, keyed by QBO's discount account, names it.
DISCOUNT_ITEM = "discount_item"


def discount_mapping(db: Session, item_id) -> QBOMapping | None:
    """The mapping of a local item a QuickBooks Online discount came in on;
    its qbo_id is QBO's discount account (empty when QBO named none)."""
    if not item_id:
        return None
    return (
        db.query(QBOMapping)
        .filter(
            QBOMapping.entity_type == DISCOUNT_ITEM,
            QBOMapping.slowbooks_id == item_id,
        )
        .first()
    )


def discount_item_ids(db: Session) -> set[int]:
    """Every local item a QuickBooks Online discount came in on."""
    return {
        sid
        for (sid,) in db.query(QBOMapping.slowbooks_id).filter(
            QBOMapping.entity_type == DISCOUNT_ITEM
        )
    }


def is_discount_item(db: Session, item_id) -> bool:
    """Whether a local item is one a QuickBooks Online discount came in on:
    a line on it may carry a negative price (routes/invoices/helpers.py,
    refuse_negative_lines)."""
    return discount_mapping(db, item_id) is not None


def ledger_posting(db: Session, txn_type: str, qbo_id) -> QBOMapping | None:
    """The ledger import's mapping for QuickBooks Online transaction
    `txn_type` #qbo_id ("Invoice", "Sales Receipt"), when it posted that
    transaction. The report named types with spaces ("Sales Receipt");
    either spelling matches."""
    wanted = txn_type.lower().replace(" ", "")
    qbo_id = str(qbo_id)
    for mapping in db.query(QBOMapping).filter(
        QBOMapping.entity_type == "ledger",
        QBOMapping.qbo_id.endswith(f":{qbo_id}", autoescape=True),
    ):
        kind, _, mapped_id = mapping.qbo_id.partition(":")
        if mapped_id == qbo_id and kind.lower().replace(" ", "") == wanted:
            return mapping
    return None
