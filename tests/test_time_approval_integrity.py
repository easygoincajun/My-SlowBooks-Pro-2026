"""Time-approval integrity (SlowBooks-Pro-Testing issue 100).

Three ways the approval workflow could be bypassed or misattributed:

1. Approve took the approver from the request body, so any caller could
   stamp an approval in anyone's name. It now comes from the signed-in
   principal (the same name the audit log records), and the body field is
   accepted but ignored for backward compatibility.
2. PUT /api/time-entries/{id} could move status straight to "approved",
   sidestepping /submit and /approve. PUT no longer changes status.
3. PUT/DELETE blocked only a pay-run lock; an entry already costed to a
   job (job_cost_id set) could still be edited or deleted out from under
   its posted labor cost. Both are now blocked with a clear message.
"""

from datetime import datetime, timezone

from app.models.time_entries import TimeEntry, TimeEntryStatus


def _hourly(client, cost_rate=None):
    body = {
        "first_name": "Dana",
        "last_name": "Field",
        "pay_type": "hourly",
        "pay_rate": 30,
    }
    if cost_rate is not None:
        body["cost_rate"] = cost_rate
    r = client.post("/api/employees", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _log(client, emp_id, day="2026-07-11", hours=8, **extra):
    r = client.post(
        "/api/time-entries",
        json={"employee_id": emp_id, "date": day, "hours_regular": hours, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── 1. Approver comes from the signed-in principal, not the body ──────────


def test_approve_attributes_to_the_signed_in_principal_not_the_body(client):
    emp = _hourly(client)
    entry = _log(client, emp)
    # The authenticated test client signs in as the default admin.
    r = client.post(
        f"/api/time-entries/{entry['id']}/approve",
        json={"approved_by": "Someone Else"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    # The spoofed name in the body is ignored; the acting user is recorded.
    assert body["approved_by"] == "admin"


def test_approve_without_a_body_still_records_the_principal(client):
    emp = _hourly(client)
    entry = _log(client, emp)
    r = client.post(f"/api/time-entries/{entry['id']}/approve", json={})
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == "admin"


# ── 2. PUT may not change status ──────────────────────────────────────────


def test_put_cannot_move_status_to_approved(client):
    emp = _hourly(client)
    entry = _log(client, emp)
    r = client.put(f"/api/time-entries/{entry['id']}", json={"status": "approved"})
    assert r.status_code == 400, r.text
    assert "submit" in r.json()["detail"].lower()
    assert "approve" in r.json()["detail"].lower()
    # The entry is untouched — still a draft, never approved.
    after = next(
        e for e in client.get("/api/time-entries").json() if e["id"] == entry["id"]
    )
    assert after["status"] == "draft"
    assert after["approved_by"] is None


def test_put_may_resend_the_current_status_unchanged(client):
    # Clients that round-trip the whole entry (status included) still work,
    # as long as they do not try to change it.
    emp = _hourly(client)
    entry = _log(client, emp)
    r = client.put(
        f"/api/time-entries/{entry['id']}",
        json={"status": "draft", "notes": "fixed a typo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"
    assert r.json()["notes"] == "fixed a typo"


def test_status_still_moves_through_submit_and_approve(client):
    emp = _hourly(client)
    entry = _log(client, emp)
    assert (
        client.post(f"/api/time-entries/{entry['id']}/submit").json()["status"]
        == "submitted"
    )
    assert (
        client.post(f"/api/time-entries/{entry['id']}/approve", json={}).json()[
            "status"
        ]
        == "approved"
    )


# ── 3. PUT/DELETE blocked once costed to a job ─────────────────────────────


def _code(client, code="06-100", name="Framing labor", cost_type="labor"):
    # Seed the cost-type chart (with offset accounts) the labor posting needs.
    assert client.post("/api/cost-types/setup-offsets").status_code == 200
    r = client.post(
        "/api/cost-codes",
        json={"code": code, "name": name, "cost_type": cost_type},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _costed_entry(client, seed_customer):
    code_id = _code(client)
    job = client.post(
        "/api/jobs", json={"customer_id": seed_customer.id, "name": "Barn"}
    ).json()
    emp = _hourly(client, cost_rate=40)
    entry = _log(client, emp, job_id=job["id"], cost_code_id=code_id)
    client.post(f"/api/time-entries/{entry['id']}/submit")
    client.post(f"/api/time-entries/{entry['id']}/approve", json={})
    posted = client.post(f"/api/time-entries/{entry['id']}/post-to-job")
    assert posted.status_code == 200, posted.text
    return entry["id"]


def test_put_blocked_when_the_entry_is_costed_to_a_job(
    client, seed_accounts, seed_customer
):
    te_id = _costed_entry(client, seed_customer)
    r = client.put(f"/api/time-entries/{te_id}", json={"hours_regular": 9})
    assert r.status_code == 400, r.text
    assert "costed to a job" in r.json()["detail"]


def test_delete_blocked_when_the_entry_is_costed_to_a_job(
    client, seed_accounts, seed_customer
):
    te_id = _costed_entry(client, seed_customer)
    r = client.delete(f"/api/time-entries/{te_id}")
    assert r.status_code == 400, r.text
    assert "costed to a job" in r.json()["detail"]


# ── QuickBooks-imported approval names still display (#323) ────────────────


def test_imported_approval_name_still_displays(client, db_session):
    emp = _hourly(client)
    entry = TimeEntry(
        employee_id=emp,
        date=datetime(2026, 7, 11).date(),
        hours_regular=8,
        status=TimeEntryStatus.APPROVED,
        approved_by="QuickBooks import",
        approved_at=datetime.now(timezone.utc),
        source="qb_desktop",
    )
    db_session.add(entry)
    db_session.commit()
    listed = client.get("/api/time-entries").json()
    row = next(e for e in listed if e["id"] == entry.id)
    assert row["approved_by"] == "QuickBooks import"
    assert row["source"] == "qb_desktop"
