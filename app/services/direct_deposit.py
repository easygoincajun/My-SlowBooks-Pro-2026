# ============================================================================
# Direct deposit — the originating details an ACH file needs, from Settings
# ----------------------------------------------------------------------------
# nacha_export builds the file from an `originating` dict. Until 2.25 that
# dict came whole in the request body, so the file could only be made by an
# API client that knew the bank's numbers (discussion #211). Settings keeps
# them now (Settings -> Direct Deposit); this module reads them, fills what
# can be derived, and names what is still missing in the words of the
# fields a person types them into.
# ============================================================================

from sqlalchemy.orm import Session

from app.models.bank_accounts import PrenoteStatus
from app.models.payroll import PayRun
from app.services.settings_service import get_all_settings
from app import config

# originating key -> the Settings field it comes from, as labelled on the page
SETTING_LABELS = {
    "immediate_destination": "Bank routing number",
    "company_account": "Company account number",
}


class MissingOrigination(ValueError):
    """Settings the file cannot be built without; str() names them."""

    def __init__(self, labels: list[str]):
        self.labels = labels
        super().__init__(
            "Direct deposit is not set up: enter "
            + " and ".join(labels)
            + " under Settings → Direct Deposit (ACH)."
        )


def _digits(text) -> str:
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def originating_from_settings(db: Session, overrides: dict | None = None) -> dict:
    """The `originating` dict for nacha_export: the saved settings, with any
    value the caller sent on top, and the derivable fields filled.

    Raises MissingOrigination when the bank's routing number or the company
    account is neither saved nor sent."""
    s = get_all_settings(db)
    sent = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}

    ein = _digits(s.get("company_tax_id") or config.EMPLOYER_EIN)
    routing = _digits(sent.get("immediate_destination") or s.get("ach_bank_routing"))
    orig = {
        "immediate_destination": routing,
        "destination_name": sent.get("destination_name")
        or s.get("ach_bank_name")
        or "BANK",
        # The bank assigns both identifiers; "1" + EIN is the usual form.
        "immediate_origin": sent.get("immediate_origin")
        or s.get("ach_immediate_origin")
        or ("1" + ein if ein else ""),
        "company_id": sent.get("company_id")
        or s.get("ach_company_id")
        or ("1" + ein if ein else ""),
        "origin_name": sent.get("origin_name") or s.get("company_name") or "",
        "company_name": sent.get("company_name")
        or s.get("company_name")
        or config.COMPANY_NAME,
        # The ODFI is the company's own bank: the first 8 digits of its routing
        # number. A caller may still send its own.
        "originating_dfi_id": _digits(sent.get("originating_dfi_id")) or routing[:8],
        "company_account": sent.get("company_account")
        or s.get("ach_company_account")
        or "",
        "effective_date": sent.get("effective_date"),
    }
    missing = [label for key, label in SETTING_LABELS.items() if not orig.get(key)]
    if missing:
        raise MissingOrigination(missing)
    return orig


def pays_by_direct_deposit(employee) -> bool:
    """True when the employee has a direct-deposit account the file would
    credit: active, and not in a prenote waiting period."""
    return any(
        a.is_active and a.prenote_status != PrenoteStatus.PENDING
        for a in (getattr(employee, "bank_accounts", None) or [])
    )


def direct_deposit_stubs(run: PayRun) -> list:
    """The stubs of `run` the ACH file pays."""
    return [s for s in run.stubs if s.employee and pays_by_direct_deposit(s.employee)]
