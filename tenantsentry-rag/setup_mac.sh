#!/bin/bash
# One-time setup after copying the PropTech folder to a Mac.
# Run from inside tenantsentry-app/tenantsentry-rag:
#   chmod +x setup_mac.sh && ./setup_mac.sh

set -e
cd "$(dirname "$0")"

echo "==> Checking Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Install it first: https://brew.sh"
    exit 1
fi

echo "==> Installing OCR dependencies (tesseract, poppler)..."
brew list tesseract >/dev/null 2>&1 || brew install tesseract
brew list poppler >/dev/null 2>&1 || brew install poppler

echo "==> Removing stale Windows virtualenv (if any)..."
if [ -d ".venv" ]; then
    rm -rf .venv
fi

echo "==> Checking Python version..."
PY=python3
if command -v python3.12 >/dev/null 2>&1; then
    PY=python3.12
fi
$PY --version

echo "==> Creating fresh virtualenv..."
$PY -m venv .venv
source .venv/bin/activate

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Checking .env..."
if [ ! -f ".env" ]; then
    echo "WARNING: .env not found. Copy it from your Windows machine (it's gitignored)."
else
    if grep -q "^TESSERACT_CMD=" .env; then
        echo "NOTE: .env has a TESSERACT_CMD entry pointing to a Windows path."
        echo "      Comment it out or remove it — Homebrew puts tesseract on PATH automatically."
    fi
fi

echo ""
echo "==> Done. Start the server with: ./run_local.sh"
