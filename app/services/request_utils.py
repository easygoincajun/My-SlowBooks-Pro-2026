# ============================================================================
# Shared request helpers used across route modules.
# ============================================================================

import unicodedata
from urllib.parse import quote

from fastapi import Request


def client_ip(request: Request) -> str:
    """The client address as uvicorn reports it, for the audit tables.

    Deliberately reads request.client only. uvicorn already rewrites it from
    X-Forwarded-For when the socket peer is in FORWARDED_ALLOW_IPS, and
    ignores the header otherwise, so trust is decided in one place; reading
    the header here again would let a client forge the audited address on
    any deploy that is reachable without a proxy. 45 chars fits IPv6."""
    client = request.client
    return (client.host if client else "")[:45]


# Letters NFKD leaves whole (no accent to take off), spelled the way a person
# types them on a plain keyboard.
_ASCII_PLAIN = {
    "Ł": "L",
    "ł": "l",
    "Đ": "D",
    "đ": "d",
    "Ð": "D",
    "ð": "d",
    "Ø": "O",
    "ø": "o",
    "Æ": "AE",
    "æ": "ae",
    "Œ": "OE",
    "œ": "oe",
    "ß": "ss",
    "Þ": "Th",
    "þ": "th",
    "ı": "i",
    "Ħ": "H",
    "ħ": "h",
}
# Characters the quoted ASCII name can't carry: a quote or backslash ends or
# escapes it, ";" starts the next parameter, and "%" makes a reader that
# percent-decodes the name (the desktop shell does) fail.
_FALLBACK_UNSAFE = frozenset('"\\;%')


def _file_name_text(name: str) -> str:
    """The name without what no file name can hold: a path separator
    becomes "-", a control character goes."""
    name = (name or "").replace("/", "-").replace("\\", "-")
    return "".join(ch for ch in name if unicodedata.category(ch) != "Cc")


def _ascii_file_name(name: str) -> str:
    """Plain ASCII for a reader without RFC 6266: accents taken off
    ("Café" -> "Cafe", "Łódź" -> "Lodz"), anything else "_"."""
    out = []
    for ch in name:
        for part in unicodedata.normalize("NFKD", _ASCII_PLAIN.get(ch, ch)):
            if unicodedata.combining(part):
                continue
            ok = " " <= part <= "~" and part not in _FALLBACK_UNSAFE
            out.append(part if ok else "_")
    return "".join(out)


def file_name(*parts) -> str:
    """A file name from what the file is and who it is for, "_" between the
    parts and "-" between a name's words: ("W-2", 2026, "Lena Ortiz") ->
    "W-2_2026_Lena-Ortiz". Blank parts are left out. Pass the result to
    content_disposition, which makes any name safe to send."""
    words = ("-".join(str(p).split()) for p in parts if p is not None)
    return "_".join(w for w in words if w)


def content_disposition(filename: str, disposition: str = "inline") -> str:
    """A Content-Disposition header value for a file named after something a
    person typed — a customer's name, an invoice or bill number.

    The raw name used to go into the header as it was. A header is sent as
    Latin-1, so an accented name reached the browser as bytes it read the
    wrong way, and a name with a letter Latin-1 lacks ("Łódź Signs") made
    the whole request fail with a 500 (2.17.3 exploratory). RFC 6266 /
    RFC 5987: ``filename*=UTF-8''…`` carries the exact name, percent-
    encoded, and every current browser uses it; ``filename="…"`` is a
    plain-ASCII stand-in for any reader that doesn't (and it is what the
    desktop shell reads)."""
    name = _file_name_text(filename)
    return (
        f'{disposition}; filename="{_ascii_file_name(name)}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )
