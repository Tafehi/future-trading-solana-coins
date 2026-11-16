#!/usr/bin/env bash
# verify_clone.sh — test a FRESH CLONE of HEAD, the way CI sees the repo. Run before every push:
#   scripts/verify_clone.sh && git push origin main
# Catches files that exist locally but were never committed (DEBUG D-4: .gitignore hid app/data/).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY="$(pwd)/.venv/bin/python"
D="$(mktemp -d)"
trap 'rm -rf "$D"' EXIT
git clone -q . "$D/repo" || exit 1
cd "$D/repo" || exit 1
"$PY" scripts/checks.py || exit 1
"$PY" -m pytest app/tests -q -p no:cacheprovider || exit 1
echo "fresh clone of $(git rev-parse --short HEAD): checks and tests PASS"
