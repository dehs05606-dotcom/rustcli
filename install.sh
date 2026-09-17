#!/usr/bin/env bash
# Install FullAgent from source. Pure Python — no build step, no native
# binary, every platform with Python 3.9+.
set -euo pipefail

REPO="dehs05606-dotcom/rustcli"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 (3.9+) is required" >&2
  exit 1
fi

echo ">> Installing FullAgent from ${REPO} ..."
python3 -m pip install --upgrade "git+https://github.com/${REPO}.git"
echo ">> Installed. Run: fullagent"
fullagent --version 2>/dev/null || true
