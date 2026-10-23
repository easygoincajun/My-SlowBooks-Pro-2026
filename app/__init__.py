# Single source of truth for the application version: app/main.py reports
# it on the FastAPI app and /health, /api/system serves it to the frontend
# footer, and the Windows release workflow parses it for the installer.
# This fork's builds are the upstream release they carry plus a fourth
# number of their own (2.26.0.1, .2, …): newer than that release, so the
# update check never offers the upstream build over this one.
__version__ = "2.26.0.1"
