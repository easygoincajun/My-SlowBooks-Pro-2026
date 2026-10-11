"""Automatic backups on the schedule set in Settings.

The first comes at the first scheduled time after the schedule is saved; one
missed while the app was closed runs at the next start; a failed one is
tried again an hour later; only the newest N automatic backups are kept,
and manual backups are never deleted.
"""

from datetime import datetime, timedelta

import pytest

from app.services import auto_backup, backup_service
from tests.test_backups_per_company import (  # noqa: F401 (fixture)
    _backup,
    _listed,
    books,
)

SAT_3PM = datetime(2026, 10, 10, 15, 0)  # a Saturday


def _sched(frequency, since=SAT_3PM, **kw):
    return {
        **auto_backup.DEFAULT_SCHEDULE,
        "frequency": frequency,
        "since": since.isoformat(),
        **kw,
    }


def _ok(at):
    return {"at": at.isoformat(), "ok": True, "message": "x.db"}


def test_off_is_never_due():
    assert auto_backup.next_backup_at(_sched("off"), None) is None


def test_daily_runs_at_its_time_not_when_it_is_saved():
    s = _sched("daily", time="23:00")
    assert auto_backup.next_backup_at(s, None) == datetime(2026, 10, 10, 23, 0)
    ran = datetime(2026, 10, 10, 23, 0, 30)
    assert auto_backup.next_backup_at(s, _ok(ran)) == datetime(2026, 10, 11, 23, 0)


def test_weekly_runs_on_its_day():
    s = _sched("weekly", time="09:30", weekday=0)  # Monday
    assert auto_backup.next_backup_at(s, None) == datetime(2026, 10, 12, 9, 30)
    ran = datetime(2026, 10, 12, 9, 30, 10)
    assert auto_backup.next_backup_at(s, _ok(ran)) == datetime(2026, 10, 19, 9, 30)


def test_every_few_hours_counts_from_the_last_one():
    s = _sched("hours", every_hours=6)
    assert auto_backup.next_backup_at(s, None) == SAT_3PM + timedelta(hours=6)
    ran = datetime(2026, 10, 10, 21, 5)
    assert auto_backup.next_backup_at(s, _ok(ran)) == ran + timedelta(hours=6)


def test_a_backup_missed_while_closed_is_due_at_once():
    s = _sched("daily", time="23:00")
    ran = datetime(2026, 10, 10, 23, 0)
    # closed Sunday night, opened Monday morning
    due = auto_backup.next_backup_at(s, _ok(ran))
    assert due < datetime(2026, 10, 12, 8, 0)


def test_a_failed_backup_is_tried_again_an_hour_later():
    s = _sched("daily", time="23:00")
    failed = {"at": "2026-10-10T23:00:00", "ok": False, "message": "drive gone"}
    assert auto_backup.next_backup_at(s, failed) == datetime(2026, 10, 11, 0, 0)


@pytest.fixture
def scheduled(books, TestSession, monkeypatch):
    clock = {}

    class _Clock(datetime):  # backup names carry the time they were taken
        @classmethod
        def now(cls, tz=None):
            return clock.get("now") or datetime.now(tz)

    monkeypatch.setattr(backup_service, "datetime", _Clock)

    def run(at):
        clock["now"] = at
        return auto_backup.run_if_due(now=at, session_factory=TestSession)

    return run


def test_a_due_backup_is_taken_and_old_ones_cleaned_up(books, client, scheduled):
    manual = _backup(client)
    auto_backup.set_schedule(_sched("hours", every_hours=1, keep=2), now=SAT_3PM)
    assert scheduled(SAT_3PM + timedelta(minutes=30)) is None  # not yet

    made = []
    for hour in (1, 2, 3):
        record = scheduled(SAT_3PM + timedelta(hours=hour, minutes=1))
        assert record["ok"], record
        made.append(record["message"])
        assert backup_service.is_auto_backup(made[-1])

    listed = _listed(client)
    assert set(listed) == {manual, made[1], made[2]}  # oldest automatic one gone
    assert listed[made[2]]["backup_type"] == "auto"
    assert listed[manual]["backup_type"] == "manual"


def test_a_failure_is_recorded_and_nothing_is_cleaned_up(
    books, client, scheduled, tmp_path
):
    drive = tmp_path / "drive"
    drive.mkdir()
    client.put("/api/backups/folder", json={"folder": str(drive)})
    auto_backup.set_schedule(_sched("hours", every_hours=1), now=SAT_3PM)
    drive.rmdir()

    record = scheduled(SAT_3PM + timedelta(hours=2))
    assert record["ok"] is False and "not available" in record["message"]
    info = client.get("/api/backups/schedule").json()
    assert info["last_run"] == record
    assert info["next_run"] == (SAT_3PM + timedelta(hours=3)).isoformat()


def test_a_backup_already_running_elsewhere_is_left_alone(books, scheduled):
    auto_backup.set_schedule(_sched("hours", every_hours=1), now=SAT_3PM)
    auto_backup._lock_path().touch()
    try:
        assert scheduled(SAT_3PM + timedelta(hours=2)) is None
    finally:
        auto_backup._lock_path().unlink()


def test_the_schedule_is_checked_when_saved(books, client):
    r = client.put("/api/backups/schedule", json={"frequency": "daily", "time": "25:00"})
    assert r.status_code == 422
    r = client.put(
        "/api/backups/schedule",
        json={"frequency": "weekly", "time": "22:15", "weekday": 6, "keep": 8},
    )
    assert r.status_code == 200, r.text
    info = r.json()
    assert (info["frequency"], info["time"], info["weekday"], info["keep"]) == (
        "weekly",
        "22:15",
        6,
        8,
    )
    assert datetime.fromisoformat(info["next_run"]).weekday() == 6
