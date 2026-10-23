"""2.24.0 round 4: NEW-167 (medium; the owner's blocker on the Mac). The
invitation's box selected all of its text on any click, with no Copy button,
so the one-time password could not be copied on its own, and the password
mixed 0/O and 1/l/I. Now the address, the username and the password are
three lines with a Copy button each, the whole invitation sits under them
selectable like any text, and the password is drawn from the readable
alphabet the copy's passphrase uses. The accountant's copy note gets the
same lines.
"""

from pathlib import Path

from app.services.auth import LOOKALIKE_CHARS, READABLE_ALPHABET, one_time_password

ROOT = Path(__file__).resolve().parents[1]


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_one_time_password_reads_without_look_alikes():
    assert set(LOOKALIKE_CHARS) == set("0O1lI")
    assert len(READABLE_ALPHABET) == 57
    assert not set(READABLE_ALPHABET) & set(LOOKALIKE_CHARS)
    seen = set()
    for _ in range(300):
        pw = one_time_password()
        assert len(pw) == 16 and set(pw) <= set(READABLE_ALPHABET), pw
        seen.add(pw)
    assert len(seen) == 300


def test_the_invitations_password_is_the_readable_one_and_signs_in(client):
    from app.models.users import ROLE_ACCOUNTANT

    passwords = []
    for i in range(5):
        r = client.post(
            "/api/users/invite", json={"username": f"cpa{i}", "role": ROLE_ACCOUNTANT}
        )
        assert r.status_code == 201, r.text
        pw = r.json()["password"]
        assert len(pw) == 16 and set(pw) <= set(READABLE_ALPHABET), pw
        passwords.append(pw)
    assert len(set(passwords)) == 5
    client.post("/api/auth/logout")
    r = client.post(
        "/api/auth/login", json={"username": "cpa4", "password": passwords[-1]}
    )
    assert r.status_code == 200, r.text
    # a sign-in that must set its password reads nothing else yet
    assert client.get("/api/customers").status_code == 403


def test_the_copys_passphrase_shares_the_alphabet():
    from app.services.accountants_copy import _PASSPHRASE_ALPHABET, _passphrase

    assert _PASSPHRASE_ALPHABET is READABLE_ALPHABET
    p = _passphrase()
    assert len(p) == 24 and set(p) <= set(READABLE_ALPHABET)


def test_each_line_of_the_invitation_has_its_own_copy_button():
    js = _src("app/static/js/settings.js")
    block = js[
        js.index("async sendInvite(e) {") : js.index("async copyInvite(which) {")
    ]
    assert "this.select()" not in block
    for which, label in (
        ("address", "Address"),
        ("username", "Username"),
        ("password", "One-time password"),
    ):
        assert (
            f"copyLineHtml('invite-{which}', '{label}', r.{which}, "
            f"\"SettingsPage.copyInvite('{which}')\")" in block
        )
    assert 'id="invite-note" readonly' in block
    assert "SettingsPage.copyInvite('note')" in block
    assert (
        "copyToClipboard(inv[which] || '', labels[which] || 'Text', $(`#invite-${which}`))"
        in js
    )


def test_the_copys_note_copies_the_same_way():
    js = _src("app/static/js/companies.js")
    block = js[
        js.index("async sendToAccountant(e) {") : js.index("async copySent(which) {")
    ]
    assert "this.select()" not in block
    assert (
        "copyLineHtml('sent-file', 'File', r.file_name, \"CompaniesPage.copySent('file')\")"
        in block
    )
    assert (
        "copyLineHtml('sent-passphrase', 'Passphrase', r.passphrase, "
        "\"CompaniesPage.copySent('passphrase')\")" in block
    )
    assert 'id="sent-note" readonly' in block
    assert "CompaniesPage.copySent('note')" in block
    assert (
        "copyToClipboard(sent[which] || '', labels[which] || 'Text', $(`#sent-${which}`))"
        in js
    )


def test_no_box_selects_all_of_its_text_on_a_click():
    for path in sorted((ROOT / "app/static/js").glob("*.js")):
        assert "this.select()" not in path.read_text(encoding="utf-8"), path.name


def test_the_copy_line_is_a_label_the_text_and_a_button_of_its_own():
    utils = _src("app/static/js/utils.js")
    helper = utils[utils.index("function copyLineHtml(id, label, value, handler) {") :][
        :700
    ]
    assert "user-select:all" in helper
    assert 'aria-label="Copy the ${escapeHtml(label.toLowerCase())}"' in helper
    assert 'onclick="${handler}">Copy</button>' in helper
    # the Copy fallback selects a text box's own value, not a range over it
    sel = utils[utils.index("function _selectElementText(el) {") :][:500]
    assert "el.focus(); el.select(); return true;" in sel
