"""Analytics says what its figures count (2.17.3 exploratory test, W-L12;
2.22.3, #281 and its gate's NEW-73).

September's "Revenue $742.20" sat beside a $10.0M Profit & Loss for the
same month, unlabelled: it was invoices dated in the period and paid in
full. Since 2.22.3 the four KPI cards, the trend and the doughnut are the
ledger's (accrual, as the P&L) on one basis, and revenue by customer keeps
the paid basis as its own section. The screen and the PDF say which is
which; the first test pins what the code computes, so the labels cannot
drift from it.
"""

from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "analytics.js"
PERIOD = "start_date=2026-09-01&end_date=2026-09-30"


def _invoice(client, customer_id, when, rate, tax_rate="0"):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": customer_id,
            "date": when,
            "tax_rate": tax_rate,
            "lines": [{"description": "work", "quantity": 1, "rate": rate}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _pay(client, customer_id, inv, when):
    r = client.post(
        "/api/payments",
        json={
            "customer_id": customer_id,
            "date": when,
            "amount": inv["total"],
            "allocations": [{"invoice_id": inv["id"], "amount": inv["total"]}],
        },
    )
    assert r.status_code == 201, r.text


def test_revenue_is_paid_invoices_dated_in_the_period(
    client, seed_accounts, seed_customer
):
    cid = seed_customer.id
    paid_sept = _invoice(client, cid, "2026-09-10", "742.20")
    _pay(client, cid, paid_sept, "2026-09-12")
    _invoice(client, cid, "2026-09-11", "10000.00")  # issued, unpaid
    paid_in_sept_for_aug = _invoice(client, cid, "2026-08-20", "300.00")
    _pay(client, cid, paid_in_sept_for_aug, "2026-09-02")

    dash = client.get(f"/api/analytics/dashboard?{PERIOD}").json()
    assert sum(dash["revenue_by_customer"].values()) == 742.20
    pl = client.get(f"/api/reports/profit-loss?{PERIOD}").json()
    assert float(pl["total_income"]) == 10742.20  # accrual: the two differ
    # the KPI card is the P&L's figure, on the ledger basis
    assert dash["totals"]["basis"] == "ledger"
    assert dash["totals"]["revenue"] == 10742.20


def test_the_screen_labels_the_basis():
    js = JS.read_text(encoding="utf-8")
    assert "Terms.text('Revenue (ledger)')" in js
    assert "Revenue (paid invoices)" not in js
    assert ">Expenses (ledger)<" in js
    assert "${escapeHtml(Terms.text(this.BASIS_NOTE))}" in js
    assert "count every posting in the ledger dated in the period (accrual)" in js
    assert "so its total can be less than the revenue above" in js
    # the cards read the server's totals, never a sum of their own
    assert "const totals = data.totals || {};" in js
    assert "this._sum(data.revenue_by_customer)" not in js
    assert 'title="${escapeHtml(marginTitle)}"' in js and "${marginText}" in js


def test_the_pdf_labels_the_basis():
    from app.services.pdf_service import _jinja_env
    from app.services.terminology import terms_for

    def render(totals):
        return _jinja_env.get_template("analytics_pdf.html").render(
            dashboard={
                "totals": totals,
                "revenue_by_customer": {"Acme": 742.2},
                "expenses_by_category": {},
                "revenue_trend": {"2026-09": 10742.2},
                "ar_aging": {},
                "ap_aging": {},
                "dso": 0,
                "cash_forecast": [],
                "customer_profit": {},
            },
            period={"name": "month", "start": "2026-09-01", "end": "2026-09-30"},
            company={"company_name": "Explore Signs & Co"},
            terms=terms_for({}),
            company_logo_data_uri="",
        )

    html = render(
        {
            "basis": "ledger",
            "revenue": 10742.2,
            "expenses": 468.0,
            "net_income": 10274.2,
            "margin_pct": 95.6,
        }
    )
    assert "Revenue (ledger)" in html and "Revenue (paid invoices)" not in html
    assert "$10,742.20" in html and "95.6%" in html  # the totals, not the paid $742.20
    assert "Expenses (ledger)" in html
    assert "a paid basis" in html and "every posting in the ledger" in html
    # the trend's column is the ledger's too (NEW-76 / W-1)
    assert '<th class="num">Revenue (ledger)</th>' in html
    assert "Paid Revenue" not in html
    # no revenue: a dash, not 0.0%
    html = render(
        {
            "basis": "ledger",
            "revenue": 0.0,
            "expenses": 48.0,
            "net_income": -48.0,
            "margin_pct": None,
        }
    )
    assert "&mdash;" in html and "0.0%" not in html


def test_revenue_by_customer_is_before_sales_tax(client, seed_accounts, seed_customer):
    """A paid $1,000 invoice at 10% tax: the customer's revenue is $1,000,
    as the ledger booked it to income (the $100 went to Sales Tax Payable),
    not the $1,100 collected (2.22.3 gate, NEW-79: the note said revenue by
    customer can total less than the ledger's revenue, and tax made it
    more)."""
    cid = seed_customer.id
    inv = _invoice(client, cid, "2026-09-10", "1000.00", tax_rate="0.10")
    assert float(inv["total"]) == 1100.0 and float(inv["subtotal"]) == 1000.0
    _pay(client, cid, inv, "2026-09-12")
    dash = client.get(f"/api/analytics/dashboard?{PERIOD}").json()
    assert dash["revenue_by_customer"] == {seed_customer.name: 1000.0}
    assert dash["totals"]["revenue"] == 1000.0
    assert dash["customer_profit"][seed_customer.name] == {"revenue": 1000.0}
