"""Items to reorder (#261, 2.22.2).

`reorder_point` was on the item and its form, and `GET /api/items/low-stock`
returned the shortage, but no page called it and the dashboard had no
inventory card: whoever buys parts found out an item was low by selling the
last one. The Items page now counts the short items in a "To reorder"
filter, listing each with what's on hand, the reorder point and how many
short; off by default, and absent when nothing is short.
"""

from decimal import Decimal

from app.models.items import Item, ItemType


def test_the_low_stock_route_says_how_many_short(client, db_session, seed_accounts):
    db_session.add_all(
        [
            Item(
                name="Bolt",
                item_type=ItemType.PRODUCT,
                rate=0,
                track_inventory=True,
                reorder_point=Decimal("10"),
                quantity_on_hand=Decimal("3"),
            ),
            Item(
                name="Nut",
                item_type=ItemType.PRODUCT,
                rate=0,
                track_inventory=True,
                reorder_point=Decimal("5"),
                quantity_on_hand=Decimal("12"),
            ),
        ]
    )
    db_session.commit()
    rows = client.get("/api/items/low-stock").json()
    assert [
        (r["name"], Decimal(r["reorder_point"]), Decimal(r["shortage"])) for r in rows
    ] == [("Bolt", Decimal("10"), Decimal("7"))]


def test_the_items_page_has_the_filter_off_by_default_and_only_when_something_is_short():
    src = open("app/static/js/items.js", encoding="utf-8").read()
    assert "_reorderOnly: false," in src
    assert "API.get('/items/low-stock')" in src
    assert (
        "To reorder (${low.length})" in src and 'aria-pressed="${reorderOnly}"' in src
    )
    assert (
        "const reorderBtn = low.length\n" in src
    )  # nothing drawn when nothing is short
    assert "ItemsPage._reorderOnly && low.length > 0" in src
    assert "Reorder point</th>" in src and "Short by</th>" in src
    assert "At or under the reorder point" in src
    assert "Nothing to reorder in this list." in src
