#!/usr/bin/env bash
# Build a single-file FullAgent executable with PyInstaller.
#
# The result is a native binary for the machine that builds it — run this
# on Linux x86_64 to get a Linux x86_64 binary; there is no cross-build.
# Source installs (install.sh / pip) remain the supported path everywhere
# else, Termux included, which cannot run a glibc binary at all.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY="${PYTHON:-python3}"

echo ">> Step 1/3: build dependencies"
$PY -m pip install --upgrade --quiet pyinstaller -r requirements.txt

echo ">> Step 2/3: clean previous build output"
rm -rf build dist fullagent.spec

echo ">> Step 3/3: freeze"
# Every fullagent module is named as a hidden import. Several of them are
# imported lazily inside functions, and a module the static scan misses
# would only surface as an ImportError on the first /command that needs
# it. Listing them is deliberate: --collect-submodules would do the same
# job by importing the package inside PyInstaller's isolated child, which
# drags in unrelated site-packages and is fragile for no benefit here.
HIDDEN=()
for f in fullagent/*.py; do
  m="$(basename "$f" .py)"
  case "$m" in __init__|__main__) continue ;; esac
  HIDDEN+=(--hidden-import "fullagent.$m")
done

# cryptography is excluded on purpose. Nothing in fullagent imports it;
# it only reaches the graph through urllib3's optional pyopenssl contrib,
# which urllib3 2.x does not use — TLS goes through the stdlib ssl module.
# Leaving it in makes PyInstaller run its collect_submodules hook, and a
# distro-built cryptography can hard-crash that hook (pyo3 panic) without
# the binary needing a byte of it.
$PY -m PyInstaller \
  --onefile \
  --name fullagent \
  "${HIDDEN[@]}" \
  --exclude-module cryptography \
  --console \
  main.py

echo ""
echo ">> Built: $HERE/dist/fullagent"
"$HERE/dist/fullagent" --version || true
