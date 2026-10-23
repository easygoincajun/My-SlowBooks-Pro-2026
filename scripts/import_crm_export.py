"""Import the in-house CRM's CSV export into a SlowBooks company.

    # 1. plan: writes <export>/match/{customers,items,accounts}.csv and
    #    report.txt; changes nothing in the books
    python scripts/import_crm_export.py ~/Downloads/crm-data-export --company "My Books" --create

    # 2. edit the match files (rename, merge, pick accounts), plan again
    python scripts/import_crm_export.py ~/Downloads/crm-data-export --company "My Books"

    # 3. import: all of it in one transaction, or nothing. --bank-balance
    #    sets the payments' bank account to its real balance on --as-of
    python scripts/import_crm_export.py ~/Downloads/crm-data-export --company "My Books" \
        --import --bank-balance 2311.55 --as-of 2026-10-11

--company names a desktop company (the company list under SLOWBOOKS_DATA_DIR);
--create makes it first, empty, when there is none by that name.
--database-url points at any database instead (a server's PostgreSQL).
See app/services/crm_import.py for what is read and how.
"""

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _company_url(name: str, create: bool) -> str:
    from app.services import company_service

    entry = next(
        (
            c
            for c in company_service.manifest_list_companies()
            if c["name"].lower() == name.lower()
        ),
        None,
    )
    if entry is None:
        if not create:
            sys.exit(f"No company named {name!r}. Add --create to make it.")
        made = company_service.manifest_create_company(name)
        if not made.get("success"):
            sys.exit(made.get("error"))
        print(f"Created company {name!r} ({made['file']}).")
        entry = made
    return "sqlite:///" + company_service.company_db_path(entry["file"]).as_posix()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("export", type=Path, help="the folder holding the CRM's CSV files")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--company", help="desktop company name")
    target.add_argument("--database-url", help="SQLAlchemy URL of the books")
    ap.add_argument(
        "--create", action="store_true", help="create the company if missing"
    )
    ap.add_argument(
        "--import", dest="do_import", action="store_true", help="post the plan"
    )
    ap.add_argument(
        "--bank-balance",
        type=Decimal,
        help="the real balance of the account payments go to; the difference "
        "posts to Opening Balance Equity",
    )
    ap.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date.today(),
        help="the date of --bank-balance (YYYY-MM-DD, default today)",
    )
    ap.add_argument(
        "--tax-agency",
        action="append",
        default=[],
        metavar="NAME=RATE",
        help="a sales tax agency and its rate in percent, e.g. 'Louisiana State=5'; "
        "repeat for each (state, parish). Set before the import posts.",
    )
    args = ap.parse_args()

    if not (args.export / "invoices.csv").exists():
        sys.exit(f"{args.export} has no invoices.csv.")
    url = args.database_url or _company_url(args.company, args.create)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import enable_sqlite_tuning
    from app.services import crm_import

    engine = create_engine(url)
    if url.startswith("sqlite"):
        enable_sqlite_tuning(engine)
    with Session(engine) as db:
        plan = crm_import.build_plan(db, args.export)
        out = crm_import.write_match_files(plan, args.export)
        print(crm_import.report(plan))
        print(f"Match files: {out}")
        if not args.do_import:
            print("Dry run: nothing was written to the books. Add --import to post.")
            return 1 if plan.problems else 0
        if plan.problems:
            print("Not imported: fix the problems above first.")
            return 1
        try:
            if args.tax_agency:
                crm_import.set_tax_agencies(db, args.tax_agency)
            made = crm_import.apply_plan(db, plan)
            if args.bank_balance is not None:
                txn = crm_import.true_up_bank(db, plan, args.bank_balance, args.as_of)
                made["bank adjustment"] = 1 if txn else 0
            db.commit()
        except Exception:
            db.rollback()
            print("Import failed; nothing was written.")
            raise
    print("Imported: " + ", ".join(f"{n} {what}" for what, n in made.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
