#!/usr/bin/env python3
"""Pass A of the vocabulary audit: measure what the server actually sends.

The static guards in tests/test_terminology.py prove every page literal is
wrapped and every T() key resolves. They cannot see the other end of the
chain — the sentences the server sends back, the PDFs it renders, the
emails it composes — because none of that is page text. This walks a
running server in BOTH company types, captures every string the API hands
a client (JSON values, PDF text, HTML text), and reports the ones that
still carry a business word in nonprofit mode. A string that reads the
same in both modes and exists verbatim in the source tree is a code leak;
one that only exists in the data is a name somebody typed.

    SLOWBOOKS_QA_PASSWORD=<the scratch company's admin password> \\
        python3 scripts/audit/vocab_walk.py --base http://127.0.0.1:3777

Read-only by construction: GET routes, the 404 path of parameterised GET
routes, the two preview endpoints, and PUT /api/settings for company_type
(restored on exit). Nothing else is called.
"""

from __future__ import annotations

import argparse
import os
import html
import http.cookiejar
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.terminology import PROTECTED_WORDS  # noqa: E402

# Business words and their derived forms. The dictionary keys are phrases;
# a leak is any whole-word appearance of the base terms, so "Invoiced",
# "Invoicing" and "Invoice #" count even though no key spells them.
BASE_TERMS = (
    "Customer",
    "Invoice",
    "Sales Receipt",
    "Job",
    "Class",
    "Income",
    "Equity",
    "Profit & Loss",
    "P&L",
    "Receivable",
    "A/R",
)
_DERIVED = r"(?:Customers?|Invoic(?:e|es|ed|ing)|Sales Receipts?|Jobs?|Class(?:es)?|Income|Equity|Profit & Loss|P&L|Receivables?|A/R)"
BUSINESS_RE = re.compile(r"(?<![\w/])" + _DERIVED + r"(?![\w/])", re.IGNORECASE)
PROTECTED_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in PROTECTED_WORDS) + r")\b", re.I
)
IDENT_RE = re.compile(r"^[a-z][a-z0-9]*([._-][a-z0-9]+)*$")
# {{ invoice.invoice_number }} is a variable name, not a sentence: check 21
# requires exactly that expression to survive, and the first cut of this
# instrument flagged it (both agents, 2.13.1 gate)
JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)


def prose_of(s: str) -> str:
    """The parts of a string a reader sees: template expressions removed."""
    return JINJA_RE.sub(" ", s)


SKIP_PREFIX = (
    "/api/auth",
    "/api/backups",
    "/api/companies",
    "/api/ai",
    "/api/simplefin",
    "/api/updates",
    "/api/system/update",
)
NOT_FOUND_ID = 999999
# Strings that carry a business word by design and are not vocabulary: the
# seeded chart's account names ("Service Income", "Accounts Receivable")
# and the state tax tables' reference notes. The chart is the company's
# data; renaming it is theirs to do.
SEEDED_DATA = ("app/seed/", "app/services/state_tax/")
TAX_TERM_RE = re.compile(r"\bIncome Tax\b", re.I)
CHART_NAMES = {"Accounts Receivable", "Opening Balance Equity", "Service Income"}


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "*/*"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(req, timeout=120) as r:
                return r.status, r.headers.get("content-type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("content-type", ""), e.read()

    def json(self, method, path, payload=None):
        status, ctype, body = self.call(method, path, payload)
        try:
            return status, json.loads(body) if body else None
        except ValueError:
            return status, None


def strings_in(obj, path="$"):
    """Every string VALUE in a JSON document, with its path. Keys are the
    API's identifiers and are never shown to a user by name."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings_in(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            yield from strings_in(v, f"{path}[{i}]")


PDFTOTEXT = shutil.which("pdftotext")
PDFS_SKIPPED: list[str] = []


def pdf_text(data: bytes) -> list[str]:
    """Text of a PDF via poppler's pdftotext. Without poppler the PDF surface
    is NOT walked — recorded by name in the summary, never a quiet zero
    (2.13.1 gate: both agents' boxes lacked it, and the first cut died with
    an error that never said 'pdftotext')."""
    if not PDFTOTEXT:
        return []
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
    out = subprocess.run(
        [PDFTOTEXT, "-layout", f.name, "-"], capture_output=True, text=True
    )
    Path(f.name).unlink(missing_ok=True)
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def html_text(data: bytes) -> list[str]:
    s = data.decode("utf-8", "replace")
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "\n", s)
    return [html.unescape(ln).strip() for ln in s.splitlines() if ln.strip()]


def classify(s: str) -> str:
    if IDENT_RE.match(s):
        return "identifier"
    if " " in s or s[-1:] in ".!?:":
        return "sentence"
    return "label"


def chart_names(c: Client) -> set[str]:
    """Every account name in this company's chart, whoever created it: the
    seed, the product on demand (4800 Late Fee Income appears after the first
    late fee — skytech, 2.13.1 gate), or a person. An account's name is the
    company's data and is never a vocabulary leak."""
    status, rows = c.json("GET", "/api/accounts?active_only=false")
    if status != 200 or not isinstance(rows, list):
        status, rows = c.json("GET", "/api/accounts")
    return {r.get("name", "").strip() for r in rows or [] if isinstance(r, dict)}


def harvest_ids(c: Client, spec: dict) -> dict[str, list[int]]:
    """resource segment -> ids seen in its list endpoint."""
    ids: dict[str, list[int]] = defaultdict(list)
    for path, ops in spec["paths"].items():
        if (
            "get" not in ops
            or "{" in path
            or not path.startswith("/api/")
            or path.startswith(SKIP_PREFIX)
        ):
            continue
        status, body = c.json("GET", path)
        if status != 200:
            continue
        rows = (
            body
            if isinstance(body, list)
            else (body.get("items") if isinstance(body, dict) else None)
        )
        if isinstance(rows, list):
            seg = path.split("/")[2]
            for row in rows[:5]:
                if isinstance(row, dict) and isinstance(row.get("id"), int):
                    ids[seg].append(row["id"])
    return ids


def fill(path: str, ids: dict[str, list[int]], missing: bool) -> list[str]:
    """Concrete URLs for a parameterised path: one with real ids, one with
    an id that cannot exist (the 404 sentence)."""
    params = re.findall(r"\{(\w+)\}", path)
    seg = path.split("/")[2]
    out = []
    if missing:
        url = path
        for p in params:
            url = url.replace(
                "{" + p + "}", str(NOT_FOUND_ID) if p.endswith("id") else "nonexistent"
            )
        return [url]
    url = path
    for p in params:
        res = p[:-3] + "s" if p.endswith("_id") else seg
        pool = ids.get(res) or ids.get(res.rstrip("s")) or ids.get(seg)
        if not pool or not p.endswith("id"):
            return []
        url = url.replace("{" + p + "}", str(pool[0]))
    out.append(url)
    return out


def walk(c: Client, spec: dict, ids: dict) -> dict[str, list[tuple[str, str, str]]]:
    """route -> [(kind, where, string)]"""
    seen: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for path, ops in sorted(spec["paths"].items()):
        if (
            "get" not in ops
            or not path.startswith("/api/")
            or path.startswith(SKIP_PREFIX)
        ):
            continue
        urls = (
            [path]
            if "{" not in path
            else fill(path, ids, False) + fill(path, ids, True)
        )
        for url in urls:
            status, ctype, body = c.call("GET", url)
            key = f"GET {path}" + (
                "  [404 path]"
                if str(NOT_FOUND_ID) in url or "nonexistent" in url
                else ""
            )
            if "pdf" in ctype:
                if not PDFTOTEXT:
                    PDFS_SKIPPED.append(key)
                    continue
                for ln in pdf_text(body):
                    seen[key].append(("pdf", f"{status}", ln))
            elif "html" in ctype:
                for ln in html_text(body):
                    seen[key].append(("html", f"{status}", ln))
            else:
                try:
                    doc = json.loads(body) if body else None
                except ValueError:
                    continue
                for where, s in strings_in(doc):
                    seen[key].append(("json", f"{status} {where}", s))
    # the two read-only previews the template editor and the send dialog use
    inv = (ids.get("invoices") or [None])[0]
    if inv:
        status, body = c.json(
            "POST",
            "/api/email-templates/preview",
            {"invoice_id": inv, "subject_template": "", "body_template": ""},
        )
        for where, s in strings_in(body):
            seen["POST /api/email-templates/preview (defaults)"].append(
                ("json", f"{status} {where}", s)
            )
        status, tpl = c.json("GET", "/api/email-templates")
        for t in (tpl or []) if isinstance(tpl, list) else []:
            status, body = c.json(
                "POST",
                "/api/email-templates/preview",
                {
                    "invoice_id": inv,
                    "subject_template": t.get("subject_template", ""),
                    "body_template": t.get("body_template", ""),
                },
            )
            for where, s in strings_in(body):
                seen[f"POST /api/email-templates/preview ({t.get('name')})"].append(
                    ("json", f"{status} {where}", s)
                )
    return seen


def source_backed(literal: str, files: list[Path]) -> str | None:
    """file:line if the literal (or a >= 12-char fragment) is in the tree."""
    frag = literal.strip()
    if len(frag) < 8:
        # "Invoice" is seven characters and appears in a hundred files; an
        # attribution for a word that short points somewhere arbitrary and
        # reads as better evidence than it is (skytech, 2.13.1 gate)
        return None
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        i = text.find(frag)
        if i < 0 and len(frag) > 30:
            i = text.find(frag[:30])
        if i >= 0:
            # as_posix: on Windows relative_to() renders backslashes, and the
            # seeded-data exemption compares against "app/seed/" (skytech,
            # 2.13.1 gate — nine chart names read as leaks there)
            return f"{f.relative_to(ROOT).as_posix()}:{text.count(chr(10), 0, i) + 1}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument(
        "--password",
        default=os.environ.get("SLOWBOOKS_QA_PASSWORD"),
        help="the scratch company's admin password; prefer SLOWBOOKS_QA_PASSWORD "
        "in the environment so it never lands in a shell history or a commit",
    )
    ap.add_argument(
        "--out", type=Path, default=ROOT / "scripts/audit/vocab_walk_report.json"
    )
    args = ap.parse_args()

    if not args.password:
        ap.error("no password: pass --password or set SLOWBOOKS_QA_PASSWORD")
    c = Client(args.base)
    status, body = c.json("POST", "/api/auth/login", {"password": args.password})
    assert status == 200, f"login: {status} {body}"
    status, spec = c.json("GET", "/openapi.json")
    status, settings = c.json("GET", "/api/settings")
    original = settings.get("company_type", "business")
    ids = harvest_ids(c, spec)
    print(
        f"ids harvested for {len(ids)} resources; {sum(len(v) for v in ids.values())} ids"
    )

    try:
        c.json("PUT", "/api/settings", {"company_type": "business"})
        accounts = chart_names(c)
        business = walk(c, spec, ids)
        c.json("PUT", "/api/settings", {"company_type": "nonprofit"})
        nonprofit = walk(c, spec, ids)
    finally:
        c.json("PUT", "/api/settings", {"company_type": original})

    files = [
        p
        for p in ROOT.glob("app/**/*")
        if p.suffix in (".py", ".js", ".html", ".json") and "__pycache__" not in str(p)
    ]
    files.sort(key=lambda p: (p.suffix != ".py", str(p)))

    report = []
    for route, rows in nonprofit.items():
        biz = {(k, w, s) for k, w, s in business.get(route, [])}
        for kind, where, s in rows:
            if not BUSINESS_RE.search(prose_of(s)) or classify(s) == "identifier":
                continue
            same_in_business = (kind, where, s) in biz
            src = source_backed(s, files) if same_in_business else None
            if (
                re.search(
                    r"/(invoices|estimates)/\{[a-z_]+\}/(pdf|print-preview|email-preview)",
                    route,
                )
                or "/email-templates/preview" in route
            ):
                # the document names ITSELF (donor_documents.document_label): a
                # nonprofit's program-fee invoice prints Invoice by design, and
                # only a flagged pledge prints Pledge. The walk's seeded
                # documents are unflagged, so the word here is the rule working.
                verdict = "document face (per document, by design)"
            elif re.sub(r"^\d{3,5}\s+", "", s.strip()) in accounts or any(
                s.strip().startswith(name) for name in accounts if len(name) > 6
            ):
                # an account name from the company's own chart (report rows
                # read "1100 Accounts Receivable   $35,280.36" in a PDF)
                verdict = "chart data (account name)"
            elif src and (
                src.startswith(SEEDED_DATA)
                or TAX_TERM_RE.search(s)
                or s.strip() in CHART_NAMES
            ):
                # a seeded account name or a tax-table note: the company's
                # own words, correctly left alone (the 2.9.1 audit's
                # "Owner's Equity" call)
                verdict = "seeded data"
            elif src:
                verdict = "CODE LEAK"
            else:
                verdict = "data or dynamic" if same_in_business else "changed"
            report.append(
                {
                    "route": route,
                    "kind": kind,
                    "where": where,
                    "text": s[:200],
                    "class": classify(s),
                    "unchanged_from_business": same_in_business,
                    "source": src,
                    "verdict": verdict,
                }
            )

    args.out.write_text(json.dumps(report, indent=1))
    leaks = [r for r in report if r["verdict"] == "CODE LEAK"]
    dyn = [r for r in report if r["verdict"] == "data or dynamic"]
    routes = defaultdict(list)
    for r in leaks:
        routes[r["route"]].append(r)
    print(
        f"\nstrings captured: business {sum(len(v) for v in business.values())}, nonprofit {sum(len(v) for v in nonprofit.values())}"
    )
    seeded = [
        r
        for r in report
        if r["verdict"] in ("seeded data", "chart data (account name)")
    ]
    faces = [r for r in report if r["verdict"].startswith("document face")]
    if not PDFTOTEXT:
        print(
            f"\nPDFs NOT WALKED: pdftotext (poppler) is not installed here — "
            f"{len(set(PDFS_SKIPPED))} PDF routes skipped. Install poppler to cover the "
            "printed documents; a zero without it is not a pass on that surface."
        )
    print(
        f"business words in nonprofit output: {len(report)} — {len(leaks)} source-backed CODE LEAKS across {len(routes)} routes, {len(seeded)} seeded data, {len(faces)} document-face, {len(dyn)} data/dynamic\n"
    )
    for route, rs in sorted(routes.items()):
        print(f"== {route}")
        shown = set()
        for r in rs:
            key = r["text"]
            if key in shown:
                continue
            shown.add(key)
            print(f"   [{r['kind']}] {r['text'][:110]!r}\n        <- {r['source']}")
    print(f"\nreport: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
