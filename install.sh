#!/usr/bin/env bash
set -euo pipefail

REPO="dehs05606-dotcom/rsclitest"
VERSION="v1.0.0"
BIN="aia-agent"

detect_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "aarch64" ;;
        *) echo "unsupported" ;;
    esac
}

detect_os() {
    local os
    os="$(uname -s)"
    case "$os" in
        Linux) echo "linux" ;;
        Darwin) echo "darwin" ;;
        *) echo "unsupported" ;;
    esac
}

main() {
    local os arch
    os="$(detect_os)"
    arch="$(detect_arch)"

    if [[ "$os" == "unsupported" || "$arch" == "unsupported" ]]; then
        echo "Unsupported platform: $(uname -s) $(uname -m)"
        echo "Build from source:"
        echo "  git clone https://github.com/$REPO.git"
        echo "  cd rsclitest && cargo build --release"
        exit 1
    fi

    local url="https://github.com/$REPO/releases/download/$VERSION/$BIN"
    local install_dir="/usr/local/bin"

    echo "Downloading $BIN $VERSION ($os/$arch)..."
    if command -v sudo &>/dev/null; then
        curl -sSfL "$url" -o "/tmp/$BIN" || {
            echo "Download failed"; exit 1
        }
        chmod +x "/tmp/$BIN"
        sudo mv "/tmp/$BIN" "$install_dir/$BIN"
    else
        curl -sSfL "$url" -o "$install_dir/$BIN" 2>/dev/null || {
            echo "Need sudo or run as root"; exit 1
        }
        chmod +x "$install_dir/$BIN"
    fi

    echo "Installed to $install_dir/$BIN"
    echo ""
    echo "Setup API key:"
    echo "  export OPENCODE_API_KEY=your_key_here"
    echo "  export AIA_PROVIDER=opencode"
    echo "  export AIA_MODEL=deepseek-v4-flash-free"
    echo ""
    echo "Run:"
    echo "  aia-agent chat"
}

main "$@"
