"""An import's error list comes whole, and the page can save it (discussion
#349).

The Transaction Detail by Account and categorized-sheet importers cut their
list at twenty and said "… and 14,070 more", and the person asked whether
there was a way to see the whole report. There was not. Both lists come
whole now, as the IIF import's already did (2.23.4 gate, W-6); the page
counts them and offers Save the list, a text file of every line.
"""

import io
from datetime import date
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_qb_desktop_ledger import _chart

ROOT = Path(__file__).resolve().parents[1]
HEADER = ",,,,,Type,,Date,,Num,,Adj,,Name,,Memo,,Clr,,Split,,Debit,,Credit,,Original Amount,,Balance"


def _ledger_with_one_sided_checks(n):
    """A report run for the bank account alone: each check is in the file
    once, with its other side missing, so none of them balance."""
    rows = [HEADER, ",Operating Checking 4321,,,,,,,,,,,,,,,,,,,,,,,,,,"]
    for i in range(n):
        rows.append(
            f",,,,,Check,,01/{(i % 28) + 1:02d}/2024,,{1001 + i},,,,Harbor Properties,,"
            f"rent,,,,Rent,,,,100.00,,-100.00,,-100.00"
        )
    rows.append(
        f",Total Operating Checking 4321,,,,,,,,,,,,,,,,,,,,0.00,,{n * 100:.2f},,,,-{n * 100:.2f}"
    )
    return ("\n".join(rows) + "\n").encode("utf-8")


def _upload(client, data, name):
    return client.post(
        "/api/csv/import/qb-report",
        files={"file": (name, io.BytesIO(data), "text/csv")},
    )


def test_the_ledger_report_lists_every_transaction_that_does_not_balance(
    client, db_session, seed_accounts
):
    _chart(db_session)
    r = _upload(
        client, _ledger_with_one_sided_checks(25), "Transaction Detail by Account.csv"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected"] == "ledger", body
    assert body["journals"] == 0
    assert len(body["errors"]) == 25, body["errors"][-3:]
    assert all("does not balance" in e for e in body["errors"])
    assert not any("more" in e for e in body["errors"])


def test_the_categorized_sheet_lists_every_row_it_could_not_read(
    client, db_session, seed_accounts
):
    rows = ["Bank Account Checking,Transaction Date,Memo,Payee,Account,Amount"]
    rows += [f",not a date {i},memo,Payee,6300,-1.00" for i in range(30)]
    r = _upload(client, ("\n".join(rows) + "\n").encode(), "Bank Acct Template.csv")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected"] == "categorized", body
    assert body["posted"] == 0
    assert len(body["errors"]) == 30, body["errors"][-3:]
    assert all("could not be read" in e for e in body["errors"])
    assert body["errors"][0].startswith("Row 2:")
    assert body["errors"][-1].startswith("Row 31:")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_counts_the_list_and_saves_it():
    out = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "import_errors_probe.js")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    ).stdout
    got = json.loads(out)
    # many: the count, the button, and every line in the box
    assert got["many"]["count_line"] == "25 problems."
    assert got["many"]["button"] is True
    assert got["many"]["lines"] == 25
    assert got["many"]["first"].startswith("Sales Receipt 110-0900001")
    # one: no count line, no button, the line itself
    assert got["one"]["button"] is False and got["one"]["lines"] == 1
    # the IIF import's own box gets the same control
    assert got["iif"]["count_line"] == "22 problems." and got["iif"]["button"] is True
    # Save the list: a text file named for the day, every line, a toast
    # named for the local day, not UTC's (2.26.0 gate, W-30 / NEW-170)
    assert got["saved"]["name"] == f"Import-problems_{date.today().isoformat()}.txt"
    assert got["saved"]["lines"] == 25
    assert got["saved"]["toast"].startswith("Saved Import-problems_")
