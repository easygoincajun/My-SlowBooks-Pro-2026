# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SlowBooks Pro 2026: a QuickBooks 2003 Pro–style double-entry accounting app (invoicing, bills, banking, US payroll, inventory, reports). Python/FastAPI backend, vanilla-JS single-page frontend with no build step. One program runs three ways: a desktop app (PyInstaller + pywebview, one SQLite file per company), Server Edition (multi-user, same code, `--serve-lan`), and Docker/PostgreSQL.

This checkout is the fork `easygoincajun/My-SlowBooks-Pro-2026` (`origin`); `upstream` is `VonHoltenCodes/SlowBooks-Pro-2026`. Work lands on the fork's `main` through PRs (`gh pr create --repo easygoincajun/My-SlowBooks-Pro-2026 ...` — always pass `--repo`, or `gh` targets the upstream parent). Don't open PRs against upstream unless asked.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# Local dev server (SQLite, no Postgres). .env holds DATABASE_URL=sqlite:///<repo>/.dev-data/dev.db,
# SLOWBOOKS_DATA_DIR=<repo>/.dev-data, APP_DEBUG=true (auto-reload), APP_HOST=127.0.0.1.
.venv/bin/alembic upgrade head && .venv/bin/python scripts/seed_database.py   # fresh DB only
.venv/bin/python run.py                                                       # http://localhost:3001

# Tests (~4 min full suite, no network). JS behaviour tests shell out to `node`; they skip without it.
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/test_backup_folder.py::test_blank_goes_back_to_the_default
.venv/bin/python -m pytest -q -k "backup"

# CI lint gate — whole tree, run BOTH before pushing (CI fails on either)
.venv/bin/black --check app/ tests/ scripts/
.venv/bin/ruff check app/ tests/ scripts/
```

CI (`.github/workflows/ci.yml`): lint → pytest (Linux) + pytest on Windows + pip-audit + Docker build. Tests wait for lint, so a formatting miss blocks everything.

Windows installer: `gh workflow run windows.yml --repo easygoincajun/My-SlowBooks-Pro-2026 --ref main` (PyInstaller bundle → smoke test → Inno Setup). Signing steps run only when an `AZURE_CLIENT_ID` secret exists; on this fork the build is unsigned. The installer keeps upstream's `AppId`, so it installs over the official app and shares its data folder (`%LOCALAPPDATA%\SlowBooksPro`).

## Architecture

**Backend layers** (`app/`): `models/` (SQLAlchemy) → `schemas/` (Pydantic, `StrictModel` from `schemas/common.py`) → `services/` (business logic) → `routes/` (`APIRouter`, each registered in `app/main.py`). Posting to the ledger goes through `services/accounting.py` (`create_journal_entry`, `get_*_account_id` for control accounts; an item without an income account posts to the default income account).

**`app/main.py` is the policy layer**, not just wiring: auth/session middleware, scoped API tokens, the lifespan (startup security checks, schema-revision check, background tasks), and **Server Edition RBAC by URL prefix** (`_ADMIN_WRITE_PREFIXES`, `_ADMIN_ONLY_PREFIXES`). A new admin-only endpoint under an existing prefix such as `/api/backups` or `/api/settings` is gated there automatically; GETs that expose admin data call `require_admin(request)` from `routes/_roles.py`. Every HTTP error passes through the exception handlers here, which translate wording into the company's vocabulary.

**Database**: `app/database.py` builds one engine from `DATABASE_URL`. Desktop = one SQLite file per company under `SLOWBOOKS_DATA_DIR` (or the platform app-data dir), chosen by `desktop_launcher.py`'s company picker and tracked in `companies.json`; server = PostgreSQL. Schema changes need an Alembic migration in `migrations/versions/`; boot refuses a DB whose revision is behind. Company settings are rows in the `settings` table (`services/settings_service.py`), so they travel with backups and restores. Machine-level state that must *not* change on restore (backup folder, backup schedule) lives in `<backups dir>/backup-settings.json` (`services/backup_service.py`, `services/auto_backup.py`).

**Frontend** (`app/static/js/`, loaded by `index.html`, all scripts global): `app.js` is the hash router; each page is a global object (`InvoicesPage`, `SettingsPage`…) that renders HTML strings. Use `API.get/post/put/del` from `api.js` (`del`, not `delete`), `escapeHtml()` for every interpolated value, `openModal`/`closeModal`, `toast`. `combobox.js` turns item/customer/account `<select>`s into type-ahead boxes automatically; the `<select>` stays the source of truth, and option values starting `__` (e.g. `__new__`) are actions that always show. Shared sales-line math is `SalesLines` (top of `invoices.js`); inline "+ New …" helpers are `VendorQuickAdd` (`vendors.js`) and `ItemQuickAdd` (`items.js`).

**Desktop vs. browser**: one program. Desktop-only UI checks `window.pywebview.api` and hides itself otherwise; the API never assumes a window.

## Rules the tests enforce (they will fail CI)

- **Wiring audit** (`tests/test_wiring.py`): every JS API call must hit a real route, and every route needs a JS caller, unless it's listed in `_INTENTIONAL_BACKEND_ONLY` with a reason.
- **Role guard** (`tests/test_bookkeeper_ui.py`): every control shown to an admin but not to a bookkeeper must be registered (`SETTINGS_TAKEN`, `SETTINGS_TAKEN_PARTS`, …) with the request it sends, and the server must 403 it for a bookkeeper. Hidden inputs aren't "offered" and must not be registered.
- **Schema audit**: a Pydantic schema with a field named `date` must import the type as `from datetime import date as dt_date`.
- **Jinja**: every `Environment`/`SandboxedEnvironment` needs `autoescape=True`.
- Tests that import a fixture from another test module need `# noqa: F811` on each test signature that uses it (ruff), placed on the line ruff reports after `black` reformats.

## Conventions from CONTRIBUTING.md

- **Wording**: page text goes through `T()` / `Terms.text` (JS) and `terms.text()` (Python), because a nonprofit company sees Pledge/Donor instead of Invoice/Customer.
- **Scope**: US only (tax, payroll, currency).
- **Errors** never tell the reader to do something the frozen desktop app can't, e.g. run `alembic`.
- **Test fixtures**: keep fixture passwords in a named constant, never next to a `"username"` literal, because the secret scanner flags the pair.
- **Commits**: short imperative subject (≤70 chars) with the why in the body. Existing history uses an `area: summary` style (`backups: …`, `sales forms: …`).
- **Test clients**: `client` is the authenticated `TestClient` and does **not** run the app lifespan, so background threads don't start in tests. Use `unauthed_client` only for auth flows. `db_session` / `TestSession` are rolled back after each test.
