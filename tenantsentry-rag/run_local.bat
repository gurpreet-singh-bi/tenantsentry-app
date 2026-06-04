@echo off
cd /d "%~dp0"
echo ================================
echo  TenantSentry.ai — Local Dev
echo ================================
echo.
echo Starting server on http://localhost:8000
echo Press Ctrl+C to stop.
echo.
uvicorn api.main:app --reload --port 8000
