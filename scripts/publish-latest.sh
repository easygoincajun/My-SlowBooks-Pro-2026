#!/usr/bin/env bash
# ============================================================================
# Publish the update manifest for SlowBooks Pro desktop installs.
#
# Writes latest.json for the given release tag and rsyncs it to the static
# host at dl.slowbookspro.com (starbase1). Desktop installs poll it (via
# /api/system/update-check) and light the footer "Update available" badge;
# the www.slowbookspro.com download page reads it client-side to show the
# current version. Same pattern as EasyAmp's publish-flatpak.sh.
#
# Run AFTER the GitHub release is verified on a real Windows machine —
# publishing is what lights up the badge on existing installs.
#
# Usage:
#   scripts/publish-latest.sh [vX.Y.Z]          # default: newest v* tag
#   SSH_HOST=starbase2-lan is the default; SB2 is LAN-only — publish on-LAN
# ============================================================================
set -euo pipefail

TAG="${1:-$(git tag -l 'v*' --sort=-v:refname | head -1)}"
[ -n "$TAG" ] || { echo "ERROR: no v* tag found and none given" >&2; exit 1; }
VER="${TAG#v}"
SSH_HOST="${SSH_HOST:-starbase2-lan}"
WEBROOT="/var/www/dl.slowbookspro.com"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# The lowest macOS the release's Mac app opens on (its LSMinimumSystemVersion,
# set by the build runner's Homebrew libraries). Installs from 2.26.0 on skip
# an update their Mac can't open; older ones go to the install page, which
# says it.
MACOS_MINIMUM="${MACOS_MINIMUM:-15.0}"

cat > "$TMP/latest.json" <<JSON
{
  "version": "$VER",
  "macos_minimum": "$MACOS_MINIMUM",
  "download_url": "https://www.slowbookspro.com/#install",
  "notes_url": "https://github.com/VonHoltenCodes/SlowBooks-Pro-2026/releases/tag/$TAG"
}
JSON

echo ">> publishing latest.json ($VER) to $SSH_HOST:$WEBROOT ..."
rsync -avz "$TMP/latest.json" "$SSH_HOST:$WEBROOT/"
echo ">> done — live at https://dl.slowbookspro.com/latest.json"
