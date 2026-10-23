"""Draft means booked but unsent (#293).

Creating an invoice posts it to the books at once and takes tracked items
out of stock; Mark Sent changes the status and posts nothing more. A
tentative invoice read as financially inactive, so the form says what
Create does, and the docs define Draft at the same spot.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_form_says_what_create_does():
    js = (ROOT / "app" / "static" / "js" / "invoices.js").read_text(encoding="utf-8")
    assert "posts it to the books" in js
    assert "Draft means not yet marked sent" in js
    # only when creating: an edit re-posts what is already on the books
    assert '${id ? \'\' : `<div class="hint" id="inv-create-note">' in js


def test_the_docs_define_draft_where_invoicing_is_described():
    md = (ROOT / "docs" / "features.md").read_text(encoding="utf-8")
    assert "**Draft means not yet marked sent, not unposted:**" in md
    assert "Mark Sent changes the status and posts nothing more" in md


def test_a_draft_is_posted_and_mark_sent_posts_nothing_more(
    client, db_session, seed_accounts, seed_customer
):
    from app.models.transactions import Transaction

    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-09-10",
            "lines": [{"description": "work", "quantity": 1, "rate": "100.00"}],
        },
    )
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["status"] == "draft"
    posted = db_session.query(Transaction).filter_by(source_type="invoice").count()
    assert posted == 1  # on the books the moment it is created
    sent = client.post(f"/api/invoices/{inv['id']}/send")
    assert sent.status_code in (200, 201), sent.text
    assert client.get(f"/api/invoices/{inv['id']}").json()["status"] == "sent"
    db_session.expire_all()
    assert db_session.query(Transaction).count() == posted  # nothing more


def test_the_create_button_names_its_note_for_a_screen_reader():
    """VoiceOver read "Create Invoice, button" and never the note beside it
    (2.23.0 gate, NEW-89): the button describes itself by the note."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "app/static/js/invoices.js").read_text(
        encoding="utf-8"
    )
    assert (
        "data-invoice-logo-action${id ? '' : ' aria-describedby=\"inv-create-note\"'}"
        in js
    )
