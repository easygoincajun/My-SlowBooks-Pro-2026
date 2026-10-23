# ============================================================================
# Employee Self-Service Portal — cookie-authenticated, server-rendered pages.
#
# Two URL families:
#
#   /portal/{token}/*    — backward-compat entry points (emailed links,
#                          old bookmarks). Each one validates the token,
#                          sets a HttpOnly session cookie carrying it, and
#                          303-redirects to the cookieless equivalent so
#                          the URL bar no longer holds the token.
#
#   /portal/*            — the real handlers. They read the token out of
#                          the `slowbooks_portal` cookie. After the first
#                          hop the employee never sees the token in a URL
#                          again — no Referer leak, no shared-bookmark
#                          leak, no browser-history breadcrumb.
# ============================================================================

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.config import FORCE_HTTPS
from app.database import get_db
from app.models.bank_accounts import BankAccountKind, DepositType, EmployeeBankAccount
from app.models.payroll import Employee, FilingStatus, portal_token_digest
from app.models.portal_access import PortalAccess
from app.models.pto import PTOAccrual, PTOPolicy, PTORequest, PTORequestStatus, PTOType
from app.routes.pto import cancel_pto_request
from app.services import file_store
from app.services.encryption import encrypt
from app.services.nacha_export import validate_routing_number
from app.services.rate_limit import limiter
from app.services.request_utils import client_ip as _client_ip
from app.services.settings_service import get_all_settings

router = APIRouter(tags=["portal"])

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_jinja_env = Environment(autoescape=True, loader=FileSystemLoader(str(TEMPLATE_DIR)))

# Idle window: a token unused for this long is treated as revoked. Sliding
# window — every authenticated request rolls last_used forward.
PORTAL_TOKEN_IDLE_DAYS = 90

# Cookie that carries the portal token after the first claim.
PORTAL_COOKIE_NAME = "slowbooks_portal"
PORTAL_COOKIE_MAX_AGE = (
    60 * 60 * 24 * 30
)  # 30 days; idle expiry is enforced server-side

_PORTAL_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store, max-age=0",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC. SQLite returns naive timestamps even when
    we wrote them with tzinfo; PostgreSQL returns tz-aware."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _get_employee(token: str, db: Session) -> Employee:
    """Resolve a portal token to an active, non-expired employee, or HTTPException.

    Updates `portal_token_last_used` on every successful lookup. 90-day idle
    + hard expiry-at enforced. A link is found by its SHA-256: the token
    itself is not kept (app/models/payroll.py).
    """
    employee = None
    if token and len(token) <= 128:
        employee = (
            db.query(Employee)
            .filter(Employee.portal_token_hash == portal_token_digest(token))
            .first()
        )
    if not employee or not employee.is_active:
        raise HTTPException(status_code=404, detail="Portal not found")

    now = _now()
    if (
        employee.portal_token_expires_at
        and _to_utc(employee.portal_token_expires_at) < now
    ):
        raise HTTPException(status_code=410, detail="Portal token has expired")
    if employee.portal_token_last_used is not None:
        idle_cutoff = now - timedelta(days=PORTAL_TOKEN_IDLE_DAYS)
        if _to_utc(employee.portal_token_last_used) < idle_cutoff:
            raise HTTPException(status_code=410, detail="Portal token has expired")

    employee.portal_token_last_used = now
    if employee.portal_token is None:
        # Its encrypted copy was saved under a payroll key this install no
        # longer has: the link just proved itself, so keep it again under
        # the key it has now, for the cookie and for an administrator.
        employee.portal_token = token
    db.commit()
    return employee


def _branding(db: Session) -> dict:
    settings = get_all_settings(db)
    # The employee has no app session, so the logo comes through the
    # portal's own public route (it is on every invoice, so no secret); the
    # id in the query changes with each new upload.
    logo = file_store.current_logo(db)
    return {
        "company_name": settings.get("company_name") or "Employer",
        "company_logo_url": (
            f"/portal/logo?v={logo.id}" if logo is not None and not logo.missing else ""
        ),
    }


def _render(name: str, db: Session, **ctx) -> HTMLResponse:
    template = _jinja_env.get_template(f"portal/{name}")
    return HTMLResponse(
        template.render(**_branding(db), **ctx), headers=_PORTAL_HEADERS
    )


def _portal_redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303, headers=_PORTAL_HEADERS)


def _set_portal_cookie(response, token: str) -> None:
    """Stamp the response with the HttpOnly portal-session cookie."""
    response.set_cookie(
        key=PORTAL_COOKIE_NAME,
        value=token,
        max_age=PORTAL_COOKIE_MAX_AGE,
        httponly=True,
        secure=FORCE_HTTPS,
        samesite="strict",
        path="/portal",
    )


def _claim(
    token: str, redirect_to: str, db: Session, request: Request | None = None
) -> RedirectResponse:
    """Validate the URL-supplied token, stamp the cookie, redirect away.

    Records a portal_accesses row when `request` is provided — backward-
    compat token URLs route here and the operator wants to see them in
    the audit trail too.
    """
    try:
        emp = _get_employee(token, db)
    except HTTPException:
        if request is not None:
            _record_portal_access(db, request, employee_id=None, success=False)
        raise
    if request is not None:
        _record_portal_access(db, request, employee_id=emp.id, success=True)
    response = _portal_redirect(redirect_to)
    # Use the DB-resident token value rather than the URL param. Same
    # string by construction (we just matched on it), but sourcing from
    # the validated Employee row gives static analyzers a clear sanitizer
    # boundary for the cookie write (CodeQL: py/cookie-injection).
    _set_portal_cookie(response, emp.portal_token)
    return response


def _redact_portal_path(path: str) -> str:
    """Strip the portal token out of a path before it's persisted.

    Legacy /portal/<token>/... URLs carry a live bearer credential in the
    path segment. Logging it verbatim would write a working token into
    portal_accesses (visible in the admin audit UI and every pg_dump) —
    anyone with read access could then impersonate the employee. Cookieless
    route names (/portal/paystubs, /portal/profile) are short words, so a
    length heuristic cleanly distinguishes them from a ~32-char token.
    """
    parts = path.split("/")
    # ['', 'portal', '<maybe token>', ...] — redact a long 3rd segment.
    if len(parts) >= 3 and len(parts[2]) > 20:
        parts[2] = "REDACTED"
    return "/".join(parts)


def _record_portal_access(
    db: Session, request: Request, employee_id: int | None, success: bool
) -> None:
    """Insert one portal_accesses row. Swallows write failures so the audit
    write can never break the request itself."""
    try:
        db.add(
            PortalAccess(
                employee_id=employee_id,
                ip=_client_ip(request),
                user_agent=(request.headers.get("user-agent") or "")[:255],
                path=_redact_portal_path(request.url.path)[:200],
                success=success,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


def _employee_from_cookie(request: Request, db: Session) -> Employee:
    """Resolve the employee from the portal cookie, or 401 if absent / invalid.

    Records a portal_accesses row on every call — success and failure both
    show up so forensic queries can spot patterns (e.g. a sudden burst of
    401s from one IP)."""
    token = request.cookies.get(PORTAL_COOKIE_NAME)
    if not token:
        _record_portal_access(db, request, employee_id=None, success=False)
        raise HTTPException(
            status_code=401,
            detail="Portal session required — open the link from your email again",
        )
    try:
        emp = _get_employee(token, db)
    except HTTPException:
        _record_portal_access(db, request, employee_id=None, success=False)
        raise
    _record_portal_access(db, request, employee_id=emp.id, success=True)
    return emp


def _processed_stub_count(emp: Employee) -> int:
    return sum(
        1
        for stub in emp.pay_stubs
        if stub.pay_run and stub.pay_run.status.value == "processed"
    )


# ---------------------------------------------------------------------------
# Shared mutation bodies. Both the cookie-based handlers and the token-in-URL
# claim handlers funnel through these so the validation + persistence logic
# lives in exactly one place. The route wrappers differ only in how they
# resolve the employee and how they render success/error (cookie stamping is
# only needed on the token-URL path).
# ---------------------------------------------------------------------------


def _save_profile(
    emp: Employee,
    *,
    filing_status: str,
    multiple_jobs: bool,
    dependents_amount: float,
    other_income_annual: float,
    deductions_annual: float,
    extra_withholding: float,
    address1: str,
    address2: str,
    city: str,
    state: str,
    zip: str,
    db: Session,
) -> None:
    """Persist W-4 / address edits. Raises HTTPException(400) on a bad filing
    status — both route variants surface that identically."""
    try:
        emp.filing_status = FilingStatus(filing_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filing status")
    emp.multiple_jobs = bool(multiple_jobs)
    emp.dependents_amount = dependents_amount
    emp.other_income_annual = other_income_annual
    emp.deductions_annual = deductions_annual
    emp.extra_withholding = extra_withholding
    emp.address1 = address1 or None
    emp.address2 = address2 or None
    emp.city = city or None
    emp.state = state or None
    emp.zip = zip or None
    db.commit()


def _add_bank(
    emp: Employee,
    *,
    nickname: str,
    account_kind: str,
    routing_number: str,
    account_number: str,
    deposit_type: str,
    db: Session,
) -> str | None:
    """Validate + persist a new direct-deposit account.

    Returns an error message (for the ?error= redirect) when validation
    fails, or None on success. Routing numbers must pass the full ABA
    checksum, not just a 9-digit shape check, so a transposed digit is
    caught before the account can ever land in an ACH file.
    """
    routing = routing_number.strip()
    account = account_number.strip()
    if not validate_routing_number(routing):
        return "Invalid routing number"
    if not account.isdigit():
        return "Account number must be numeric"
    try:
        kind = BankAccountKind(account_kind)
        dtype = DepositType(deposit_type)
    except ValueError:
        return "Invalid selection"

    db.add(
        EmployeeBankAccount(
            employee_id=emp.id,
            nickname=nickname or None,
            account_kind=kind,
            routing_number_enc=encrypt(routing),
            account_number_enc=encrypt(account),
            account_last_four=account[-4:],
            deposit_type=dtype,
            is_active=True,
        )
    )
    db.commit()
    return None


def _error_url(page: str, message: str) -> str:
    """Build the /portal/<page>?error=... redirect target for a validation
    failure message (spaces become '+')."""
    return f"/portal/{page}?error=" + message.replace(" ", "+")


def _bank_error_url(message: str) -> str:
    return _error_url("bank", message)


def _request_pto(
    emp: Employee,
    *,
    start_date: str,
    end_date: str,
    hours: float,
    pto_type: str,
    notes: str,
    db: Session,
) -> None:
    """Persist a PTO request. Raises HTTPException(400) on a bad type or
    unparseable dates — both route variants surface that identically."""
    try:
        ptype = PTOType(pto_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid PTO type")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")

    db.add(
        PTORequest(
            employee_id=emp.id,
            start_date=start,
            end_date=end,
            hours=hours,
            pto_type=ptype,
            notes=notes or None,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# Cookieless handlers — the real implementations.
# Registered BEFORE the catch-all `/portal/{token}` so literal paths
# (`/portal/`, `/portal/paystubs`, etc.) win the routing match.
# ---------------------------------------------------------------------------


@router.get("/portal/")
@limiter.limit("30/minute")
def portal_dashboard(request: Request, db: Session = Depends(get_db)):
    emp = _employee_from_cookie(request, db)
    return _render(
        "dashboard.html",
        db,
        emp=emp,
        stub_count=_processed_stub_count(emp),
    )


@router.get("/portal/paystubs")
@limiter.limit("30/minute")
def portal_paystubs(request: Request, db: Session = Depends(get_db)):
    emp = _employee_from_cookie(request, db)
    stubs = [
        stub
        for stub in emp.pay_stubs
        if stub.pay_run and stub.pay_run.status.value == "processed"
    ]
    stubs.sort(key=lambda s: s.pay_run.pay_date, reverse=True)
    return _render("paystubs.html", db, emp=emp, stubs=stubs)


@router.get("/portal/profile")
@limiter.limit("30/minute")
def portal_profile(request: Request, saved: int = 0, db: Session = Depends(get_db)):
    emp = _employee_from_cookie(request, db)
    return _render(
        "profile.html",
        db,
        emp=emp,
        filing_statuses=list(FilingStatus),
        saved=saved,
    )


@router.post("/portal/profile")
@limiter.limit("10/minute")
def portal_profile_save(
    request: Request,
    filing_status: str = Form(...),
    multiple_jobs: bool = Form(False),
    dependents_amount: float = Form(0),
    other_income_annual: float = Form(0),
    deductions_annual: float = Form(0),
    extra_withholding: float = Form(0),
    address1: str = Form(""),
    address2: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    zip: str = Form(""),
    db: Session = Depends(get_db),
):
    emp = _employee_from_cookie(request, db)
    _save_profile(
        emp,
        filing_status=filing_status,
        multiple_jobs=multiple_jobs,
        dependents_amount=dependents_amount,
        other_income_annual=other_income_annual,
        deductions_annual=deductions_annual,
        extra_withholding=extra_withholding,
        address1=address1,
        address2=address2,
        city=city,
        state=state,
        zip=zip,
        db=db,
    )
    return _portal_redirect("/portal/profile?saved=1")


@router.get("/portal/bank")
@limiter.limit("30/minute")
def portal_bank(
    request: Request,
    saved: int = 0,
    error: str = "",
    db: Session = Depends(get_db),
):
    emp = _employee_from_cookie(request, db)
    accounts = (
        db.query(EmployeeBankAccount)
        .filter(EmployeeBankAccount.employee_id == emp.id)
        .order_by(EmployeeBankAccount.id)
        .all()
    )
    return _render(
        "bank.html",
        db,
        emp=emp,
        accounts=accounts,
        account_kinds=list(BankAccountKind),
        deposit_types=list(DepositType),
        saved=saved,
        error=error,
    )


@router.post("/portal/bank")
@limiter.limit("10/minute")
def portal_bank_add(
    request: Request,
    nickname: str = Form(""),
    account_kind: str = Form(...),
    routing_number: str = Form(...),
    account_number: str = Form(...),
    deposit_type: str = Form(...),
    db: Session = Depends(get_db),
):
    emp = _employee_from_cookie(request, db)
    error = _add_bank(
        emp,
        nickname=nickname,
        account_kind=account_kind,
        routing_number=routing_number,
        account_number=account_number,
        deposit_type=deposit_type,
        db=db,
    )
    if error:
        return _portal_redirect(_bank_error_url(error))
    return _portal_redirect("/portal/bank?saved=1")


@router.get("/portal/pto")
@limiter.limit("30/minute")
def portal_pto(
    request: Request,
    saved: int = 0,
    withdrawn: int = 0,
    error: str = "",
    db: Session = Depends(get_db),
):
    emp = _employee_from_cookie(request, db)
    accruals = (
        db.query(PTOAccrual, PTOPolicy)
        .join(PTOPolicy, PTOAccrual.policy_id == PTOPolicy.id)
        .filter(PTOAccrual.employee_id == emp.id)
        .order_by(PTOPolicy.name)
        .all()
    )
    requests = (
        db.query(PTORequest)
        .filter(PTORequest.employee_id == emp.id)
        .order_by(PTORequest.start_date.desc())
        .all()
    )
    return _render(
        "pto.html",
        db,
        emp=emp,
        accruals=accruals,
        requests=requests,
        pto_types=list(PTOType),
        saved=saved,
        withdrawn=withdrawn,
        error=error,
    )


@router.post("/portal/pto")
@limiter.limit("10/minute")
def portal_pto_request(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(...),
    hours: float = Form(0),
    pto_type: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    emp = _employee_from_cookie(request, db)
    _request_pto(
        emp,
        start_date=start_date,
        end_date=end_date,
        hours=hours,
        pto_type=pto_type,
        notes=notes,
        db=db,
    )
    return _portal_redirect("/portal/pto?saved=1")


@router.post("/portal/pto/{request_id}/withdraw")
@limiter.limit("10/minute")
def portal_pto_withdraw(
    request: Request, request_id: int, db: Session = Depends(get_db)
):
    """The employee takes back a request nobody has acted on yet. One that
    was approved is the employer's to cancel — the hours have left the bank
    and the relief is posted, so it goes through the HR page."""
    emp = _employee_from_cookie(request, db)
    req = (
        db.query(PTORequest)
        .filter(PTORequest.id == request_id, PTORequest.employee_id == emp.id)
        .first()
    )
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != PTORequestStatus.PENDING:
        return _portal_redirect(
            _error_url(
                "pto",
                "Only a request that is still pending can be withdrawn. "
                "Ask your employer to cancel it.",
            )
        )
    cancel_pto_request(db, req)
    db.commit()
    return _portal_redirect("/portal/pto?withdrawn=1")


@router.post("/portal/logout")
def portal_logout():
    """Clear the portal cookie and send the employee somewhere neutral."""
    response = _portal_redirect("/portal/")
    response.delete_cookie(PORTAL_COOKIE_NAME, path="/portal")
    return response


def _logo_or_nothing(db: Session):
    """The company's own logo, from its database, or a 204: no logo is not
    an error worth a 404 in every browser console."""
    from fastapi import Response

    logo = file_store.current_logo(db)
    try:
        response = file_store.logo_response(db, logo)
    except HTTPException:
        return Response(status_code=204, headers=_PORTAL_HEADERS)
    response.headers.update(_PORTAL_HEADERS)
    return response


@router.get("/portal/favicon.ico")
def portal_favicon(db: Session = Depends(get_db)):
    """Serve the employer's company logo as the portal favicon.

    Falls back to a 204 if no logo is configured — better than a 404 in
    every browser dev-tools console. The actual <link rel="icon"> in the
    portal templates points here, so each customer's portal carries their
    own bookmark icon. Read from the company's own database, with its real
    image type (it used to look for the file under app/static only, which a
    desktop install never wrote to, and to call every logo a PNG).
    """
    return _logo_or_nothing(db)


@router.get("/portal/logo")
def portal_logo(db: Session = Depends(get_db)):
    """The logo in the portal's header (the employee has no app session to
    fetch /api/uploads/logo/<id> with)."""
    return _logo_or_nothing(db)


# ---------------------------------------------------------------------------
# Backward-compat claim shims — the URL the employee receives by email is
# still /portal/{token}/... but each of these now sets the cookie and
# 303-redirects to the cookieless URL. After one hop the token is no longer
# in the URL bar, the browser history, or any Referer.
# ---------------------------------------------------------------------------


@router.get("/portal/{token}")
@limiter.limit("30/minute")
def portal_claim_dashboard(request: Request, token: str, db: Session = Depends(get_db)):
    return _claim(token, "/portal/", db, request)


@router.get("/portal/{token}/paystubs")
@limiter.limit("30/minute")
def portal_claim_paystubs(request: Request, token: str, db: Session = Depends(get_db)):
    return _claim(token, "/portal/paystubs", db, request)


@router.get("/portal/{token}/profile")
@limiter.limit("30/minute")
def portal_claim_profile(request: Request, token: str, db: Session = Depends(get_db)):
    return _claim(token, "/portal/profile", db, request)


@router.get("/portal/{token}/bank")
@limiter.limit("30/minute")
def portal_claim_bank(request: Request, token: str, db: Session = Depends(get_db)):
    return _claim(token, "/portal/bank", db, request)


@router.get("/portal/{token}/pto")
@limiter.limit("30/minute")
def portal_claim_pto(request: Request, token: str, db: Session = Depends(get_db)):
    return _claim(token, "/portal/pto", db, request)


# POST routes with token in the URL — process inline, stamp the cookie, then
# redirect to the cookieless URL. Browsers can't redirect a POST across paths
# cleanly, so we do the work first and 303 to the GET equivalent.
def _employee_from_token(request: Request, token: str, db: Session) -> Employee:
    """_get_employee + portal_accesses audit row, mirroring the cookie path.

    The token-URL POST handlers mutate the most sensitive data the portal
    holds (W-4 elections, direct-deposit accounts) — they must show up in
    the audit trail exactly like their cookie-based twins."""
    try:
        emp = _get_employee(token, db)
    except HTTPException:
        _record_portal_access(db, request, employee_id=None, success=False)
        raise
    _record_portal_access(db, request, employee_id=emp.id, success=True)
    return emp


def _set_cookie_on(response, emp: Employee):
    # Cookie value is read from the validated Employee row, not the URL
    # param. Same string by construction, but DB-sourced gives static
    # analyzers a clear sanitizer for the cookie write
    # (CodeQL: py/cookie-injection).
    _set_portal_cookie(response, emp.portal_token)
    return response


@router.post("/portal/{token}/profile")
@limiter.limit("10/minute")
def portal_claim_profile_save(
    request: Request,
    token: str,
    filing_status: str = Form(...),
    multiple_jobs: bool = Form(False),
    dependents_amount: float = Form(0),
    other_income_annual: float = Form(0),
    deductions_annual: float = Form(0),
    extra_withholding: float = Form(0),
    address1: str = Form(""),
    address2: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    zip: str = Form(""),
    db: Session = Depends(get_db),
):
    emp = _employee_from_token(request, token, db)
    _save_profile(
        emp,
        filing_status=filing_status,
        multiple_jobs=multiple_jobs,
        dependents_amount=dependents_amount,
        other_income_annual=other_income_annual,
        deductions_annual=deductions_annual,
        extra_withholding=extra_withholding,
        address1=address1,
        address2=address2,
        city=city,
        state=state,
        zip=zip,
        db=db,
    )
    return _set_cookie_on(_portal_redirect("/portal/profile?saved=1"), emp)


@router.post("/portal/{token}/bank")
@limiter.limit("10/minute")
def portal_claim_bank_add(
    request: Request,
    token: str,
    nickname: str = Form(""),
    account_kind: str = Form(...),
    routing_number: str = Form(...),
    account_number: str = Form(...),
    deposit_type: str = Form(...),
    db: Session = Depends(get_db),
):
    emp = _employee_from_token(request, token, db)
    error = _add_bank(
        emp,
        nickname=nickname,
        account_kind=account_kind,
        routing_number=routing_number,
        account_number=account_number,
        deposit_type=deposit_type,
        db=db,
    )
    if error:
        return _set_cookie_on(_portal_redirect(_bank_error_url(error)), emp)
    return _set_cookie_on(_portal_redirect("/portal/bank?saved=1"), emp)


@router.post("/portal/{token}/pto")
@limiter.limit("10/minute")
def portal_claim_pto_request(
    request: Request,
    token: str,
    start_date: str = Form(...),
    end_date: str = Form(...),
    hours: float = Form(0),
    pto_type: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    emp = _employee_from_token(request, token, db)
    _request_pto(
        emp,
        start_date=start_date,
        end_date=end_date,
        hours=hours,
        pto_type=pto_type,
        notes=notes,
        db=db,
    )
    return _set_cookie_on(_portal_redirect("/portal/pto?saved=1"), emp)
