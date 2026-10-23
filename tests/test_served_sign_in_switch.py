"""Choosing another company on the sign-in screen of a company whose file is
gone lands in that company (2.25.0 gate, macbase1 NEW-169, #370), and the
setup form of a company nobody has set up yet offers the other companies
(skytech W-28, #369).

On a Server Edition host serving several companies, a session signed in to
a company whose file then went missing gets the sign-in screen with the
sentence, and the Company select still lists the others. Choosing one that
this session's sign-in also opens switches it there signed in, with no
password asked: POST /api/auth/company answers `authenticated: true`. The
select's change handler then removed the screen and called promptAuth
again, which found the session signed in and drew nothing: the page stayed
on "Couldn't load this page" with the old company's name in the topbar,
nothing to type into, and a reload showed the right company.

The screen now reloads the page onto the chosen company's Home, as the
topbar's switch does. A company that asks for its own password still gets
its sign-in drawn, and a refused choice still says why on the same screen.

#369: an administrator makes a company under Companies and, before they
open it, someone on the sign-in screen chooses it. The screen became that
company's setup form; submitting was refused with the right sentence, and
the form had no Company select and no way back but clearing the cookies.
The setup form on a served host now carries the same Company select and
the same change handler as the sign-in; a server with one company, and the
desktop's first run, draw the form as before.
"""

import functools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


@functools.lru_cache(maxsize=None)
def _scenarios():
    out = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "auth_company_switch_probe.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",  # node writes UTF-8; Windows would read cp1252
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return {s["scenario"]: s for s in map(json.loads, out.stdout.splitlines())}


@needs_node
def test_a_company_the_sign_in_opens_reloads_onto_its_home_page():
    opens = _scenarios()["opens"]
    assert opens["reloads"] == 1
    assert opens["hash"] == "#/"
    # the screen is gone, and nothing drew a second one
    assert opens["overlays"] == 0
    assert opens["screen"] == "none"
    assert opens["status_checks"] == 1


@needs_node
def test_a_company_that_asks_for_its_password_gets_its_sign_in_drawn():
    asks = _scenarios()["asks"]
    assert asks["reloads"] == 0
    assert asks["hash"] == "#/reports"
    assert asks["overlays"] == 1
    assert asks["screen"] == "login"
    # promptAuth read the status again for the other company's screen
    assert asks["status_checks"] == 2
    assert "missing" in asks["message"]


@needs_node
def test_a_refused_choice_says_why_on_the_same_screen():
    refused = _scenarios()["refused"]
    assert refused["reloads"] == 0
    assert refused["overlays"] == 1
    assert refused["message"] == "This server does not serve that company."


def test_the_source_reloads_on_an_opened_company():
    src = (ROOT / "app" / "static" / "js" / "auth.js").read_text(encoding="utf-8")
    handler = src.split("function wireCompanySelect(", 1)[1]
    handler = handler.split("promptAuth(onSuccess);", 1)[0]
    assert "if (r && r.authenticated)" in handler
    assert 'window.location.hash = "#/";' in handler
    assert "window.location.reload();" in handler


@needs_node
def test_an_unset_companys_setup_form_offers_the_other_companies():
    setup = _scenarios()["setup-offers-the-others"]
    assert setup["has_select"] is True
    # choosing a company that is set up draws its sign-in in the form's place
    assert setup["reloads"] == 0
    assert setup["overlays"] == 1
    assert setup["screen"] == "login"
    assert setup["status_checks"] == 2


@needs_node
def test_the_administrators_sign_in_opens_the_company_chosen_on_the_setup_form():
    opens = _scenarios()["setup-opens"]
    assert opens["has_select"] is True
    assert opens["reloads"] == 1
    assert opens["hash"] == "#/"
    assert opens["overlays"] == 0


@needs_node
def test_a_lone_companys_setup_form_is_as_it_was():
    alone = _scenarios()["setup-alone"]
    assert alone["screen"] == "setup"
    assert alone["has_select"] is False
    assert alone["reloads"] == 0


def test_the_setup_view_shares_the_sign_ins_company_select():
    src = (ROOT / "app" / "static" / "js" / "auth.js").read_text(encoding="utf-8")
    setup_view = src.split("function setupViewHTML()", 1)[1].split(
        "function collectSetupPayload", 1
    )[0]
    assert "companySelectHTML()" in setup_view
    wire_setup = src.split("function wireSetup(", 1)[1].split(
        "function showRecoveryKey", 1
    )[0]
    assert "wireCompanySelect(overlay, onSuccess);" in wire_setup
    wire_login = src.split("function wireLogin(", 1)[1].split("// ----- setup view", 1)[
        0
    ]
    assert "wireCompanySelect(overlay, onSuccess);" in wire_login
