#!/bin/bash
# TenantSentry.ai — Local Dev (macOS/Linux)
# Mac equivalent of run_local.bat

cd "$(dirname "$0")"

echo "================================"
echo " TenantSentry.ai — Local Dev"
echo "================================"
echo ""

# Activate venv if present
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "Warning: .venv not found. Run setup_mac.sh first."
fi

echo "Starting server on http://localhost:8000"
echo "Press Ctrl+C to stop."
echo ""

uvicorn api.main:app --reload --port 8000
